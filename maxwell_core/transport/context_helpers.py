"""Scoped identity, timestamps, and reference formatting for conversation prompts."""

import contextlib
from datetime import datetime, timedelta, timezone

from identity import fill_identity, process_name
from rag_memory import MemoryRequester
from utils import _coerce_utc_datetime, _safe_int

# Puerto Rico uses Atlantic Standard Time throughout the year.
CONTEXT_TIMEZONE = timezone(timedelta(hours=-4), name="AST")


def _memory_requester_for(bot, message) -> MemoryRequester:
    """Build request scope without making tests/plugins assume bot internals."""
    checker = getattr(bot, "_is_admin", None)
    author = getattr(message, "author", None)
    try:
        is_admin = (
            bool(checker(getattr(author, "id", None))) if callable(checker) else False
        )
    except Exception:
        is_admin = False
    return MemoryRequester.from_message(message, is_admin=is_admin)


def _web_result_snippet(content: str, title: str, limit: int = 280) -> str:
    """Body-only snippet for a stored web_result row.

    The row `content` leads with the title (and, for rows written before
    2026-08-10, with the title twice — it used to be stored as the
    title-weighted embed text). The prompt line already prints the title
    from metadata, so leaving it in the snippet showed it two or three
    times and spent the char budget on repetition instead of the body.
    Drop leading lines that just repeat the title, then truncate.
    """
    text = str(content or "")
    t = str(title or "").strip()
    if t:
        lines = text.split("\n")
        i = 0
        while i < len(lines) and lines[i].strip() == t:
            i += 1
        text = "\n".join(lines[i:])
    return text.strip()[:limit]


def _format_context_timestamp(
    value, *, now: datetime | None = None, relative: bool = True
) -> str:
    """Render a stored timestamp for the prompt.

    ``relative=False`` returns ONLY the absolute local stamp, which is stable
    for a given message forever. Anything replayed on every turn (the channel
    transcript) must use it: a relative "12m ago" is recomputed against the
    current clock, so every historical line changes bytes on every request and
    the provider-side prefix cache misses on the single largest part of the
    prompt. The live current time is stated once in the volatile block instead,
    which is enough for the model to derive age.
    """
    dt = _coerce_utc_datetime(value)
    if dt is None:
        return ""
    local = dt.astimezone(CONTEXT_TIMEZONE).strftime("%a %Y-%m-%d %H:%M")
    if not relative:
        return f"{local} local"
    now = _coerce_utc_datetime(now) or datetime.now(timezone.utc)
    age_s = _safe_int((now - dt).total_seconds(), 0)
    if age_s < 0:
        rel = "just now"
    elif age_s < 60:
        rel = f"{age_s}s ago"
    elif age_s < 3600:
        rel = f"{age_s // 60}m ago"
    elif age_s < 86400:
        rel = f"{age_s // 3600}h ago"
    else:
        rel = f"{age_s // 86400}d ago"
    return f"{rel} / {local} local"


def _fill_identity_text(bot, text: str, *, live_name: bool = False) -> str:
    """Substitute identity placeholders. live_name uses the current process name."""
    overrides = {}
    if live_name:
        name = process_name(bot) if bot is not None else None
        if name:
            overrides["bot_name"] = name
    return fill_identity(
        text,
        getattr(bot, "_identity", None) if bot is not None else None,
        config=getattr(bot, "config", None) if bot is not None else None,
        **overrides,
    )


def _live_self_member(user, guild):
    """Bot's guild Member, if this turn has a guild. Never cached by us."""
    if guild is None:
        return None
    me = getattr(guild, "me", None)
    if me is not None:
        return me
    uid = getattr(user, "id", None)
    getter = getattr(guild, "get_member", None)
    if uid is None or not callable(getter):
        return None
    with contextlib.suppress(Exception):
        member = getter(uid)
        if member is not None:
            return member
    with contextlib.suppress(Exception):
        member = getter(int(uid))
        if member is not None:
            return member
    return None


def _live_account_name(user, bot_name: str | None = None) -> str:
    """Global display name / username, not a guild nick."""
    fallback = str(bot_name or "bot").strip() or "bot"
    name = str(
        getattr(user, "display_name", None) or getattr(user, "name", None) or fallback
    ).strip()
    return name or fallback


def _live_self_name(user, guild=None, bot_name: str | None = None) -> tuple[str, str]:
    """People-facing name for this room, read live from the guild member.

    Returns (name, source) where source is ``nick`` or ``account``. Guild nick
    wins when set; otherwise global display name / username. DMs have no nick.
    """
    account = _live_account_name(user, bot_name)
    member = _live_self_member(user, guild)
    if member is None:
        return account, "account"
    nick = str(getattr(member, "nick", None) or "").strip()
    if nick:
        return nick, "nick"
    shown = str(
        getattr(member, "display_name", None)
        or getattr(member, "name", None)
        or account
    ).strip()
    return shown or account, "account"


def _live_self_identity_line(user, guild=None, bot_name: str | None = None) -> str:
    """One prompt line: current name in this server (or account name in DMs)."""
    name, source = _live_self_name(user, guild, bot_name)
    account = _live_account_name(user, bot_name)
    if guild is None:
        return (
            f"Your name here: {name} (account name; this chat has no server nickname)."
        )
    guild_name = str(getattr(guild, "name", None) or "this server").strip() or (
        "this server"
    )
    if source == "nick":
        account_bit = (
            f" Account name: {account}." if account and account != name else ""
        )
        return (
            f"Your name here: {name} (server nickname in {guild_name}). "
            f"People in this server see you as {name}.{account_bit}"
        )
    return (
        f"Your name here: {name} (no server nickname in {guild_name}; "
        f"this is your account name)."
    )
