"""Mail arriving in Maxwell's notification inbox.

The four email_* tools are pull-only: he learns about a message when he
happens to run ``email_read_inbox``. That means mail is invisible unless he
already suspects it exists. This module polls IMAP in the background and
turns each new unread message into an inbox notice, so a new mail shows up
in his planner tail next to friend requests without him going looking.

Three rules keep it from nagging:

* One notice per message, ever — ``InboxStore.insert_if_absent`` plus a
  persisted UID high-water mark. Dismissing a mail makes it stay dismissed
  even while it is still UNSEEN on the server.
* The poll never marks anything read. ``BODY.PEEK`` leaves the \\Seen flag
  alone, so the mailbox looks the same to the email tools and to a human
  reading it in a real client.
* Mail from his own address is not new mail. A self-copy of something
  he sent — a server-side ``always_bcc``, a self-BCC, a list that
  reflects the post back — lands in INBOX like anything else, and used
  to be filed and announced as if a stranger had written in. On this
  install that was 13 of 36 filed messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import logging
import re
import ssl
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from utils import _atomic_json_write_sync, _load_json_safe

logger = logging.getLogger(__name__)

# Header block + first slice of the body, in one round trip per message.
_FETCH_SPEC = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] BODY.PEEK[1]<0.2048>)"

SNIPPET_CHARS = 220
SUBJECT_CHARS = 160
# Per tick. A backlog drains over several ticks instead of writing 300 inbox
# rows at once and evicting everything else in the ring.
MAX_NEW_PER_POLL = 8


def email_item_id(uid: str | int) -> str:
    return f"email_{str(uid).strip()}"


def _addr(raw: str) -> str:
    """Bare lowercase address out of a From header value."""
    return parseaddr(str(raw or ""))[1].strip().lower()


def is_ignored_sender(mail: dict, patterns: set[str]) -> bool:
    """True when this sender is on the operator's ignore list.

    Patterns are matched on the bare address: a full address
    (`noreply-dmarc-support@google.com`) or a leading-dot domain
    (`.google.com`) which matches that domain and its subdomains.

    Empty by default on purpose. Which machine mail matters is the operator's
    call, not ours — a DMARC aggregate report is pure telemetry, but a
    MAILER-DAEMON bounce means something he sent did not arrive, and both
    look identical to a heuristic.
    """
    if not patterns:
        return False
    addr = _addr(mail.get("from_addr"))
    if not addr:
        return False
    if addr in patterns:
        return True
    domain = addr.rpartition("@")[2]
    return any(
        p.startswith(".") and (domain == p[1:] or domain.endswith(p))
        for p in patterns
    )


def is_self_copy(mail: dict, own_addresses: set[str]) -> bool:
    """True when this message is one of his own, come back around.

    Compared on the bare address, so `"Maxwell" <maxwell@z3ki.dev>` and
    `maxwell@z3ki.dev` are the same sender. Both the mailbox login and the
    configured From address count as his: an install can send as one and
    receive as the other.
    """
    if not own_addresses:
        return False
    return _addr(mail.get("from_addr")) in own_addresses


def _decode_header(raw: str | None) -> str:
    """RFC 2047 → text. Malformed headers degrade to their raw bytes."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _clean(text: str, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit]


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


class MailPollState:
    """Persisted high-water mark, so a restart doesn't replay the mailbox."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "email_poll_state.json"
        self.last_uid: int = 0
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        data = await asyncio.to_thread(_load_json_safe, self.path, dict)
        if isinstance(data, dict):
            with contextlib.suppress(TypeError, ValueError):
                self.last_uid = int(data.get("last_uid") or 0)
        self._loaded = True

    async def save(self, last_uid: int) -> None:
        if last_uid <= self.last_uid:
            return
        self.last_uid = int(last_uid)
        self._loaded = True
        await asyncio.to_thread(
            _atomic_json_write_sync, self.path, {"last_uid": self.last_uid}
        )


def _connect(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    """IMAPS to the local Dovecot, whose cert is the self-signed snakeoil.

    Same trust posture as the email tools in bot_tools: verification is off
    because the target is 127.0.0.1. Point this at a remote host and you want
    a real cert and this context replaced.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    conn.login(user, password)
    return conn


def _parse_fetch_response(response: Any) -> tuple[bytes, bytes]:
    """Pull (header_bytes, body_bytes) out of imaplib's ragged shapes.

    A FETCH with two BODY.PEEK sections comes back as an alternating list of
    tuples and bare bytes separators, and servers disagree about the exact
    layout. Take the literals in order rather than trusting positions.
    """
    literals: list[bytes] = []
    for part in response or []:
        if isinstance(part, tuple):
            literals.extend(
                entry
                for entry in part[1:]
                if isinstance(entry, bytes) and entry.strip()
            )
    header = literals[0] if literals else b""
    body = literals[1] if len(literals) > 1 else b""
    return header, body


def _fetch_new_sync(
    host: str,
    port: int,
    user: str,
    password: str,
    last_uid: int,
    limit: int,
) -> list[dict]:
    """Return metadata for unread messages with UID > last_uid.

    Blocking; the caller runs it in a thread. Never mutates flags.
    """
    conn = _connect(host, port, user, password)
    try:
        conn.select("INBOX")
        # UID SEARCH is the only stable identifier — sequence numbers shift
        # whenever anything is expunged. The UID range trims the scan to what
        # arrived since the last tick; UNSEEN keeps mail he already read in a
        # real client from being announced.
        criteria = f"UID {int(last_uid) + 1}:* UNSEEN" if last_uid else "UNSEEN"
        typ, data = conn.uid("SEARCH", None, criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = [u for u in data[0].split() if u.strip()]
        if not uids:
            return []
        # Oldest first so the high-water mark advances monotonically even if
        # we stop early at `limit`.
        uids = sorted(uids, key=lambda b: int(b))
        out: list[dict] = []
        for raw_uid in uids[:limit]:
            uid = int(raw_uid)
            # "UID n:*" always returns at least one message even when nothing
            # is newer, so the server can hand back the mark itself.
            if uid <= last_uid:
                continue
            typ, response = conn.uid("FETCH", raw_uid, _FETCH_SPEC)
            if typ != "OK" or not response:
                logger.debug("Mail poll: fetch failed for uid %s", uid)
                continue
            header_bytes, body_bytes = _parse_fetch_response(response)
            parser = BytesParser(policy=policy.default)
            try:
                headers = parser.parsebytes(header_bytes)
            except Exception as e:
                # Malformed MIME header: skip this one, keep polling the rest.
                logger.warning("Mail poll: unparseable headers for uid %s: %s", uid, e)
                continue
            subject = _clean(_decode_header(headers.get("Subject")), SUBJECT_CHARS)
            from_raw = _decode_header(headers.get("From"))
            name, addr = parseaddr(from_raw)
            snippet = ""
            if body_bytes:
                text = body_bytes.decode("utf-8", errors="replace")
                if "<" in text and ">" in text:
                    text = _strip_html(text)
                snippet = _clean(text, SNIPPET_CHARS)
            out.append(
                {
                    "uid": uid,
                    "subject": subject,
                    "from_name": _clean(name, 80) or addr or "unknown sender",
                    "from_addr": addr,
                    "date": _clean(_decode_header(headers.get("Date")), 64),
                    "message_id": _clean(headers.get("Message-ID") or "", 200),
                    "snippet": snippet,
                }
            )
        return out
    finally:
        # close()/logout() raise if the server already dropped us; that must
        # not mask the result we just built.
        with contextlib.suppress(Exception):
            conn.close()
        with contextlib.suppress(Exception):
            conn.logout()


class EmailInboxPoller:
    """Background IMAP poll that files new mail as inbox notices.

    Failures are expected and cheap (Dovecot restarting, a bad password after
    a config edit). The interval backs off exponentially while the mailbox is
    unreachable and snaps back on the first success, so a broken mail setup
    costs one log line every few minutes instead of one per tick.
    """

    def __init__(
        self,
        store: Any,
        cfg: dict,
        *,
        data_dir: str | Path,
        interval: float = 120.0,
        max_backoff: float = 1800.0,
    ):
        self.store = store
        self.cfg = dict(cfg)
        # Every address that counts as "him". The login and the From address
        # can differ — an install may authenticate as one and send as another
        # — so both are his, and a self-copy from either is not new mail.
        self.ignored_senders = {
            p.strip().lower()
            for p in str(self.cfg.get("ignore_senders") or "").split(",")
            if p.strip()
        }
        self.own_addresses = {
            addr
            for addr in (
                _addr(self.cfg.get("user")),
                _addr(self.cfg.get("from_addr")),
            )
            if addr
        }
        self.interval = max(15.0, float(interval))
        self.max_backoff = max(self.interval, float(max_backoff))
        self.state = MailPollState(data_dir)
        self._failures = 0

    def configured(self) -> bool:
        return bool(self.cfg.get("password") and self.cfg.get("user"))

    def backoff_seconds(self) -> float:
        """Current wait. Doubles per consecutive failure, capped."""
        if self._failures <= 0:
            return self.interval
        return min(self.interval * (2 ** min(self._failures, 8)), self.max_backoff)

    def _build_item(self, mail: dict) -> dict:
        subject = mail.get("subject") or "(no subject)"
        sender = mail.get("from_name") or mail.get("from_addr") or "someone"
        return {
            "id": email_item_id(mail["uid"]),
            "kind": "email",
            "state": "unread",
            "actor_id": str(mail.get("from_addr") or ""),
            "actor_name": str(sender),
            "summary": f'New email from {sender}: "{subject}"',
            # `read` demotes it in the planner tail; the body itself comes
            # from email_get_message with this uid.
            "actions": ["read", "dismiss"],
            "payload": {
                "uid": str(mail["uid"]),
                "subject": subject,
                "from": mail.get("from_addr") or "",
                "date": mail.get("date") or "",
                "snippet": mail.get("snippet") or "",
                "message_id": mail.get("message_id") or "",
                "hint": (
                    f"email_get_message message_id={mail['uid']} for the full body"
                ),
            },
        }

    async def poll_once(self) -> int:
        """One tick. Returns how many new notices were filed."""
        if not self.configured():
            return 0
        await self.state.load()
        try:
            mails = await asyncio.to_thread(
                _fetch_new_sync,
                self.cfg["imap_host"],
                int(self.cfg["imap_port"]),
                self.cfg["user"],
                self.cfg["password"],
                self.state.last_uid,
                MAX_NEW_PER_POLL,
            )
        except Exception as exc:
            self._failures += 1
            logger.warning(
                "Mail poll failed (%s consecutive, next try in %.0fs): %s",
                self._failures,
                self.backoff_seconds(),
                exc,
            )
            return 0
        self._failures = 0
        if not mails:
            return 0
        filed = 0
        skipped_self = 0
        skipped_ignored = 0
        highest = self.state.last_uid
        for mail in mails:
            if is_ignored_sender(mail, self.ignored_senders):
                # Operator said this sender is not worth an inbox row. Still
                # advance the mark: the mail stays on the server and the email
                # tools can still read it, it just does not queue for
                # attention.
                skipped_ignored += 1
                highest = max(highest, int(mail["uid"]))
                continue
            if is_self_copy(mail, self.own_addresses):
                # His own message, come back around. Advance the mark past it
                # so it is not reconsidered every tick, but file nothing —
                # announcing a copy of what he just sent as new mail is how
                # he ended up narrating his own outbox.
                skipped_self += 1
                highest = max(highest, int(mail["uid"]))
                continue
            try:
                row = await self.store.insert_if_absent(self._build_item(mail))
            except Exception as exc:
                # Don't advance the mark past a message we failed to file, or
                # it is lost for good.
                logger.warning("Mail poll: could not file uid %s: %s", mail["uid"], exc)
                break
            if row is not None:
                filed += 1
            highest = max(highest, int(mail["uid"]))
        if highest > self.state.last_uid:
            await self.state.save(highest)
        if skipped_self:
            logger.info(
                "Mail poll: skipped %s self-copy message(s)", skipped_self
            )
        if skipped_ignored:
            logger.info(
                "Mail poll: skipped %s message(s) from ignored senders",
                skipped_ignored,
            )
        if filed:
            logger.info("Mail poll: %s new message(s) in the inbox", filed)
        return filed

    async def run(self) -> None:
        """Loop until cancelled. Never raises out of the tick."""
        if not self.configured():
            logger.info("Mail poll disabled: no mailbox password configured")
            return
        # Let the bot finish connecting before the first IMAP round trip.
        await asyncio.sleep(min(30.0, self.interval))
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self._failures += 1
                logger.error("Mail poll loop error: %s", exc)
            await asyncio.sleep(self.backoff_seconds())
