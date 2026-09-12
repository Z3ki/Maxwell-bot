"""User-installable Discord app for Maxwell admins.

Discord user-install is command-only: slash, message context menus, and user
context menus. Maxwell registers those with USER_INSTALL. The Custom Install
Link still gates who can click Add to my apps; invocations are also refused
unless the user is a Maxwell admin.

Channel history is not in the command payload. When Maxwell is also in the
server, we snapshot recent messages. Message context menus include the
clicked message (Discord's official way to point at channel content).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

USER_INSTALL_COMMAND_NAME = "maxwell"
USER_INSTALL_MESSAGE_ASK = "Ask Maxwell"
USER_INSTALL_MESSAGE_SUMMARIZE = "Summarize"
USER_INSTALL_USER_ASK = "Ask Maxwell"
USER_INSTALL_NAMES = frozenset(
    {
        USER_INSTALL_COMMAND_NAME,
        USER_INSTALL_MESSAGE_ASK,
        USER_INSTALL_MESSAGE_SUMMARIZE,
    }
)
# Discord: 1 original interaction response + 5 follow-ups when the app is
# only user-installed (not also in that server).
USER_INSTALL_MESSAGE_CAP = 6
USER_INSTALL_HISTORY_LIMIT = 25

_USER_INSTALL_META = {
    "integration_types": [1],
    "contexts": [0, 1, 2],
}

# integration_types 1 = USER_INSTALL. contexts 0/1/2 = guild, bot DM, GDM/DM.
USER_INSTALL_COMMANDS: list[dict[str, Any]] = [
    {
        "name": USER_INSTALL_COMMAND_NAME,
        "description": "Ask Maxwell. Maxwell admins only.",
        "type": 1,
        **_USER_INSTALL_META,
        "options": [
            {
                "name": "prompt",
                "description": "What to ask Maxwell",
                "type": 3,
                "required": True,
            },
            {
                "name": "image",
                "description": "Optional image for Maxwell to see",
                "type": 11,
                "required": False,
            },
            {
                "name": "file",
                "description": "Optional file for Maxwell to read",
                "type": 11,
                "required": False,
            },
        ],
    },
    {
        "name": USER_INSTALL_MESSAGE_ASK,
        "type": 3,
        **_USER_INSTALL_META,
    },
    {
        "name": USER_INSTALL_MESSAGE_SUMMARIZE,
        "type": 3,
        **_USER_INSTALL_META,
    },
    {
        "name": USER_INSTALL_USER_ASK,
        "type": 2,
        **_USER_INSTALL_META,
    },
]


def is_user_install_message(message: Any) -> bool:
    return bool(getattr(message, "user_install", False))


def _interaction_data(interaction: Any) -> dict[str, Any]:
    data = getattr(interaction, "data", None)
    if isinstance(data, dict):
        return data
    if data is None:
        return {}
    resolved = getattr(data, "resolved", None)
    if resolved is not None and not isinstance(resolved, dict):
        resolved = {
            "messages": getattr(resolved, "messages", None) or {},
            "users": getattr(resolved, "users", None) or {},
            "attachments": getattr(resolved, "attachments", None) or {},
            "members": getattr(resolved, "members", None) or {},
        }
    cmd_type = getattr(data, "type", None)
    cmd_type = getattr(cmd_type, "value", cmd_type)
    return {
        "name": getattr(data, "name", None),
        "type": cmd_type,
        "target_id": getattr(data, "target_id", None),
        "options": getattr(data, "options", None) or [],
        "resolved": resolved or {},
    }


def _command_name(interaction: Any) -> str:
    return str(_interaction_data(interaction).get("name") or "")


def is_user_install_command(interaction: Any) -> bool:
    if _command_name(interaction) not in USER_INSTALL_NAMES:
        return False
    itype = getattr(interaction, "type", None)
    value = getattr(itype, "value", itype)
    return value in (None, 2)


def _option_pairs(options: Any) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    for opt in options or []:
        if isinstance(opt, dict):
            pairs.append((str(opt.get("name") or ""), opt.get("value")))
        else:
            pairs.append(
                (
                    str(getattr(opt, "name", "") or ""),
                    getattr(opt, "value", None),
                )
            )
    return pairs


def _resolved_map(resolved: Any, key: str) -> dict[str, Any]:
    if not isinstance(resolved, dict):
        return {}
    raw = resolved.get(key) or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        out = {}
        for item in raw:
            iid = str(getattr(item, "id", "") or (item.get("id") if isinstance(item, dict) else "") or "")
            if iid:
                out[iid] = item
        return out
    return {}


def _lookup_resolved(mapping: dict[str, Any], key: str) -> Any:
    if not key:
        return None
    if key in mapping:
        return mapping[key]
    if key.isdigit() and int(key) in mapping:
        return mapping[int(key)]
    return mapping.get(str(key))


def parse_user_install_command(interaction: Any) -> tuple[str, list[Any]]:
    data = _interaction_data(interaction)
    prompt = ""
    attachment_ids: list[str] = []
    for name, value in _option_pairs(data.get("options")):
        if name == "prompt":
            prompt = str(value or "")
        elif name in {"image", "file"} and value is not None:
            attachment_ids.append(str(value))
    attachments: list[Any] = []
    atts = _resolved_map(data.get("resolved"), "attachments")
    for image_id in attachment_ids:
        raw = _lookup_resolved(atts, image_id)
        if raw is not None:
            attachments.append(_attachment_from_resolved(raw))
    return prompt, attachments


def parse_target_message(interaction: Any) -> Any | None:
    data = _interaction_data(interaction)
    target_id = str(data.get("target_id") or "")
    messages = _resolved_map(data.get("resolved"), "messages")
    raw = _lookup_resolved(messages, target_id)
    if raw is None and messages:
        raw = next(iter(messages.values()), None)
    if raw is None:
        return None
    return _message_from_resolved(raw, interaction)


def parse_target_user(interaction: Any) -> Any | None:
    data = _interaction_data(interaction)
    target_id = str(data.get("target_id") or "")
    users = _resolved_map(data.get("resolved"), "users")
    raw = _lookup_resolved(users, target_id)
    if raw is None and users:
        raw = next(iter(users.values()), None)
    if raw is None:
        return None
    return _user_from_resolved(raw)


def _user_from_resolved(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    uid = raw.get("id")
    name = (
        raw.get("global_name")
        or raw.get("display_name")
        or raw.get("username")
        or "user"
    )
    return SimpleNamespace(
        id=int(uid) if str(uid).isdigit() else uid,
        name=raw.get("username") or name,
        display_name=name,
        bot=bool(raw.get("bot")),
        mention=f"<@{uid}>",
    )


def _embed_text(embed: Any) -> str:
    if not isinstance(embed, dict):
        title = str(getattr(embed, "title", "") or "")
        desc = str(getattr(embed, "description", "") or "")
        url = str(getattr(embed, "url", "") or "")
    else:
        title = str(embed.get("title") or "")
        desc = str(embed.get("description") or "")
        url = str(embed.get("url") or "")
    parts = [p for p in (title, desc, url) if p]
    return "\n".join(parts)


def _message_from_resolved(raw: Any, interaction: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    author = _user_from_resolved(raw.get("author") or {})
    attachments = [
        _attachment_from_resolved(item)
        for item in (raw.get("attachments") or [])
        if item is not None
    ]
    embeds = list(raw.get("embeds") or [])
    mentions = [
        _user_from_resolved(item)
        for item in (raw.get("mentions") or [])
        if item is not None
    ]
    content = str(raw.get("content") or "")
    extra = [_embed_text(e) for e in embeds]
    extra = [t for t in extra if t]
    if extra:
        content = "\n".join([p for p in (content, *extra) if p])
    mid = raw.get("id")
    return SimpleNamespace(
        id=int(mid) if str(mid).isdigit() else mid,
        channel_id=raw.get("channel_id") or getattr(interaction, "channel_id", None),
        author=author,
        content=content,
        clean_content=content,
        attachments=attachments,
        embeds=embeds,
        stickers=[],
        mentions=mentions,
        reference=None,
        webhook_id=raw.get("webhook_id"),
        pinned=bool(raw.get("pinned")),
        tts=bool(raw.get("tts")),
        type=SimpleNamespace(name="default"),
        created_at=raw.get("timestamp"),
        jump_url="",
        bot=False,
    )


def _attachment_from_resolved(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    return SimpleNamespace(
        id=raw.get("id"),
        filename=raw.get("filename") or "image",
        url=raw.get("url") or "",
        proxy_url=raw.get("proxy_url") or raw.get("url") or "",
        content_type=raw.get("content_type"),
        size=int(raw.get("size") or 0),
        width=raw.get("width"),
        height=raw.get("height"),
        ephemeral=False,
        description=None,
        spoiler=False,
    )


def _memory_row_from_message(message: Any) -> dict[str, Any]:
    author = getattr(message, "author", None)
    content = str(getattr(message, "content", "") or "")
    atts = list(getattr(message, "attachments", None) or [])
    if atts and not content:
        names = ", ".join(str(getattr(a, "filename", "file") or "file") for a in atts[:4])
        content = f"[attachment: {names}]"
    created = getattr(message, "created_at", None)
    timestamp = ""
    if created is not None:
        iso = getattr(created, "isoformat", None)
        timestamp = iso() if callable(iso) else str(created)
    return {
        "author": getattr(author, "display_name", None)
        or getattr(author, "name", None)
        or "unknown",
        "author_id": str(getattr(author, "id", "") or ""),
        "author_is_bot": bool(getattr(author, "bot", False)),
        "content": content,
        "message_id": str(getattr(message, "id", "") or ""),
        "timestamp": timestamp,
    }


def merge_user_install_history(
    memory: list[dict] | None, extra: list[dict] | None
) -> list[dict]:
    """Keep stored channel memory; add snapshot rows Maxwell has not seen."""
    memory = list(memory or [])
    extra = list(extra or [])
    if not extra:
        return memory
    have = {str(row.get("message_id") or "") for row in memory if row.get("message_id")}
    fresh = [
        row
        for row in extra
        if str(row.get("message_id") or "") and str(row.get("message_id")) not in have
    ]
    if not memory:
        return extra
    if not fresh:
        return memory
    return fresh + memory


async def snapshot_channel_history(bot: Any, interaction: Any) -> list[dict[str, Any]]:
    """Recent channel messages when Maxwell can actually read the channel."""
    cid = getattr(interaction, "channel_id", None)
    channel = getattr(interaction, "channel", None)
    history = getattr(channel, "history", None)
    if not callable(history) and cid is not None and bot is not None:
        getter = getattr(bot, "get_channel", None)
        if callable(getter):
            with_ch = getter(int(cid) if str(cid).isdigit() else cid)
            if with_ch is not None:
                channel = with_ch
                history = getattr(channel, "history", None)
        if not callable(history):
            fetch = getattr(bot, "fetch_channel", None)
            if callable(fetch):
                try:
                    channel = await fetch(int(cid) if str(cid).isdigit() else cid)
                    history = getattr(channel, "history", None)
                except Exception:
                    history = None
    if not callable(history):
        return []
    rows: list[dict[str, Any]] = []
    try:
        result = history(limit=USER_INSTALL_HISTORY_LIMIT)
        if hasattr(result, "__aiter__"):
            rows.extend(
                [_memory_row_from_message(msg) async for msg in result]
            )
        else:
            rows.extend(_memory_row_from_message(msg) for msg in result or [])
    except Exception as e:
        logger.info("user-install channel history unavailable: %s", e)
        return []
    rows.reverse()
    return rows


def build_user_install_turn(interaction: Any) -> dict[str, Any] | None:
    """Parse slash / message / user commands into a turn dict."""
    data = _interaction_data(interaction)
    name = str(data.get("name") or "")
    cmd_type = data.get("type")
    cmd_type = getattr(cmd_type, "value", cmd_type)
    try:
        cmd_type = int(cmd_type) if cmd_type is not None else 1
    except (TypeError, ValueError):
        cmd_type = 1
    channel = getattr(interaction, "channel", None)
    channel_name = str(getattr(channel, "name", "") or "") or "this channel"
    note_bits = [
        f"User-install command ({name}) in #{channel_name}.",
        "This is a personal app command, not a server-member bot.",
        "Channel transcript may be incomplete unless Maxwell is also in this server.",
    ]
    if cmd_type == 3:
        target = parse_target_message(interaction)
        if target is None:
            return None
        prompt = (
            "Summarize this message."
            if name == USER_INSTALL_MESSAGE_SUMMARIZE
            else "Respond to this message."
        )
        note_bits.append(
            "They used a message context menu. The pointed-at message is the reply parent."
        )
        return {
            "prompt": prompt,
            "attachments": [],
            "mentions": list(getattr(target, "mentions", None) or []),
            "reference": SimpleNamespace(
                message_id=getattr(target, "id", None),
                resolved=target,
            ),
            "note": " ".join(note_bits),
            "command": name,
        }
    if cmd_type == 2:
        target = parse_target_user(interaction)
        if target is None:
            return None
        label = getattr(target, "display_name", None) or getattr(target, "name", "user")
        uid = getattr(target, "id", "")
        prompt = (
            f"Tell me about {label} (<@{uid}>). Use what you know and "
            "anything in this conversation."
        )
        note_bits.append(
            "They used a user context menu. The mentioned user is the target."
        )
        return {
            "prompt": prompt,
            "attachments": [],
            "mentions": [target],
            "reference": None,
            "note": " ".join(note_bits),
            "command": name,
        }
    prompt, attachments = parse_user_install_command(interaction)
    if not str(prompt).strip():
        return None
    return {
        "prompt": str(prompt).strip(),
        "attachments": attachments,
        "mentions": [],
        "reference": None,
        "note": " ".join(note_bits),
        "command": name,
    }


class _NoopTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class UserInstallChannelAdapter:
    def __init__(self, session: "UserInstallSession"):
        self._session = session
        self.id = session.channel_id
        self.name = session.channel_name
        self.guild = session.guild
        self._real = getattr(session.interaction, "channel", None)

    def typing(self):
        return _NoopTyping()

    def permissions_for(self, _member):
        return SimpleNamespace(
            send_messages=True,
            embed_links=True,
            attach_files=True,
            mention_everyone=False,
            use_external_emojis=True,
        )

    async def send(self, content: str | None = None, file=None, **kwargs):
        return await self._session.send(content=content, file=file, **kwargs)

    async def fetch_message(self, message_id):
        fetch = getattr(self._real, "fetch_message", None)
        if callable(fetch):
            return await fetch(message_id)
        raise LookupError("user-install has no channel history")


class UserInstallSession:
    """Send via the interaction token: 1 original + 5 follow-ups, 15 minutes."""

    def __init__(self, interaction: Any):
        self.interaction = interaction
        self.channel_id = getattr(interaction, "channel_id", None) or 0
        channel = getattr(interaction, "channel", None)
        self.channel_name = str(getattr(channel, "name", "") or "") or "user-install"
        self.guild = getattr(interaction, "guild", None)
        self._sent = 0
        self._last = None

    async def ensure_deferred(self) -> None:
        response = getattr(self.interaction, "response", None)
        is_done = getattr(response, "is_done", None)
        if callable(is_done) and is_done():
            return
        defer = getattr(response, "defer", None)
        if callable(defer):
            await defer()

    async def send(self, content: str | None = None, file=None, **kwargs):
        await self.ensure_deferred()
        kwargs.pop("stickers", None)
        kwargs.pop("reference", None)
        kwargs.pop("mention_author", None)
        text = None if content is None else str(content)
        if text == "":
            text = None
        if self._sent >= USER_INSTALL_MESSAGE_CAP:
            return await self._edit_last(text, file)
        followup = getattr(self.interaction, "followup", None)
        send = getattr(followup, "send", None)
        if not callable(send):
            raise TypeError("user-install interaction has no followup.send")
        payload: dict[str, Any] = {}
        if text is not None:
            payload["content"] = text
        if file is not None:
            payload["file"] = file
        if not payload:
            payload["content"] = "\u200b"
        sent = await send(**payload)
        self._sent += 1
        self._last = sent
        return sent

    async def _edit_last(self, text: str | None, file=None):
        last = self._last
        if last is None:
            logger.warning("user-install follow-up cap reached with nothing to edit")
            return SimpleNamespace(id=0, content=text or "", channel=None)
        if file is not None:
            logger.warning(
                "user-install follow-up cap reached; dropping extra attachment"
            )
        if text:
            prev = str(getattr(last, "content", "") or "")
            merged = (prev + "\n" + text).strip()[:2000]
            edit = getattr(last, "edit", None)
            if callable(edit):
                try:
                    updated = await edit(content=merged)
                    self._last = updated or last
                    return self._last
                except Exception:
                    logger.exception("user-install edit after cap failed")
        return last


class UserInstallMessageAdapter:
    def __init__(
        self,
        interaction: Any,
        prompt: str,
        attachments: list[Any] | None = None,
        *,
        mentions: list[Any] | None = None,
        reference: Any | None = None,
        history: list[dict] | None = None,
        note: str = "",
    ):
        self.user_install = True
        self.tool_platform = "user_install"
        self.suppress_typing = True
        self.interaction = interaction
        self._session = UserInstallSession(interaction)
        self.id = getattr(interaction, "id", 0)
        self.channel = UserInstallChannelAdapter(self._session)
        self.guild = self._session.guild
        self.author = getattr(interaction, "user", None) or SimpleNamespace(
            id=0, name="unknown", display_name="unknown", bot=False
        )
        self.content = prompt
        self.clean_content = prompt
        self.attachments = list(attachments or [])
        self.embeds = []
        self.stickers = []
        self.components = []
        self.mentions = list(mentions or [])
        self.role_mentions = []
        self.channel_mentions = []
        self.raw_mentions = []
        self.mention_everyone = False
        self.reference = reference
        self.user_install_history = list(history or [])
        self.user_install_note = note
        self.poll = None
        self.activity = None
        self.call = None
        self.webhook_id = None
        self.pinned = False
        self.tts = False
        self.nonce = None
        self.flags = 0
        self.type = SimpleNamespace(name="default")
        self.created_at = datetime.now(timezone.utc)
        self.jump_url = ""

    def typing(self):
        return _NoopTyping()

    async def reply(self, content: str | None = None, file=None, **kwargs):
        return await self._session.send(content=content, file=file, **kwargs)

    async def send(self, content: str | None = None, file=None, **kwargs):
        return await self._session.send(content=content, file=file, **kwargs)


async def _ephemeral(interaction: Any, text: str) -> None:
    response = getattr(interaction, "response", None)
    is_done = getattr(response, "is_done", None)
    if callable(is_done) and not is_done():
        send = getattr(response, "send_message", None)
        if callable(send):
            await send(text, ephemeral=True)
            return
    followup = getattr(interaction, "followup", None)
    send = getattr(followup, "send", None)
    if callable(send):
        await send(text, ephemeral=True)


async def handle_user_install_interaction(bot: Any, interaction: Any) -> bool:
    """Claim and start a user-install turn. True if this was ours."""
    if not is_user_install_command(interaction):
        return False
    user = getattr(interaction, "user", None)
    uid = getattr(user, "id", None)
    is_admin = getattr(bot, "_is_admin", None)
    if not callable(is_admin) or uid is None or not is_admin(uid):
        await _ephemeral(
            interaction,
            "Only Maxwell admins can use this user-installed app.",
        )
        return True
    turn = build_user_install_turn(interaction)
    if turn is None or not str(turn.get("prompt") or "").strip():
        await _ephemeral(interaction, "Maxwell could not read that command.")
        return True
    try:
        response = getattr(interaction, "response", None)
        is_done = getattr(response, "is_done", None)
        if not (callable(is_done) and is_done()):
            defer = getattr(response, "defer", None)
            if callable(defer):
                await defer()
    except Exception:
        logger.exception("user-install defer failed")
        return True
    history = await snapshot_channel_history(bot, interaction)
    note = str(turn.get("note") or "")
    if history:
        note = (
            note
            + f" A live snapshot of the last {len(history)} messages is in the transcript."
        ).strip()
    message = UserInstallMessageAdapter(
        interaction,
        str(turn["prompt"]).strip(),
        turn.get("attachments") or [],
        mentions=turn.get("mentions") or [],
        reference=turn.get("reference"),
        history=history,
        note=note,
    )
    spawn = getattr(bot, "_spawn_detached", None)
    on_message = getattr(bot, "on_message", None)
    if callable(spawn) and callable(on_message):
        spawn(on_message(message))
    elif callable(on_message):
        await on_message(message)
    return True
