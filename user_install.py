"""User-installable Discord app: /maxwell for Maxwell admins.

Discord user-install is command-only. Maxwell registers a single slash
command (USER_INSTALL, all interaction contexts). The Custom Install Link
still gates who can click "Add to my apps"; command invocations are also
refused unless the user is a Maxwell admin.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

USER_INSTALL_COMMAND_NAME = "maxwell"
# Discord: 1 original interaction response + 5 follow-ups when the app is
# only user-installed (not also in that server).
USER_INSTALL_MESSAGE_CAP = 6

# integration_types 1 = USER_INSTALL. contexts 0/1/2 = guild, bot DM, GDM/DM.
USER_INSTALL_COMMANDS: list[dict[str, Any]] = [
    {
        "name": USER_INSTALL_COMMAND_NAME,
        "description": "Ask Maxwell. Maxwell admins only.",
        "type": 1,
        "integration_types": [1],
        "contexts": [0, 1, 2],
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
        ],
    }
]


def is_user_install_message(message: Any) -> bool:
    return bool(getattr(message, "user_install", False))


def is_user_install_command(interaction: Any) -> bool:
    data = getattr(interaction, "data", None)
    name = None
    if isinstance(data, dict):
        name = data.get("name")
    elif data is not None:
        name = getattr(data, "name", None)
    if str(name or "") != USER_INSTALL_COMMAND_NAME:
        return False
    itype = getattr(interaction, "type", None)
    value = getattr(itype, "value", itype)
    # APPLICATION_COMMAND = 2. Also accept missing type (tests).
    return value in (None, 2)


def parse_user_install_command(interaction: Any) -> tuple[str, list[Any]]:
    data = getattr(interaction, "data", None) or {}
    if not isinstance(data, dict):
        data = {}
    prompt = ""
    image_id = ""
    for opt in data.get("options") or []:
        if not isinstance(opt, dict):
            continue
        name = str(opt.get("name") or "")
        value = opt.get("value")
        if name == "prompt":
            prompt = str(value or "")
        elif name == "image" and value is not None:
            image_id = str(value)
    attachments: list[Any] = []
    resolved = data.get("resolved") or {}
    if not isinstance(resolved, dict):
        resolved = {}
    atts = resolved.get("attachments") or {}
    if image_id and isinstance(atts, dict):
        raw = atts.get(image_id)
        if raw is None and image_id.isdigit():
            raw = atts.get(int(image_id))
        if raw is not None:
            attachments.append(_attachment_from_resolved(raw))
    return prompt, attachments


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

    async def fetch_message(self, _message_id):
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
        self.mentions = []
        self.role_mentions = []
        self.channel_mentions = []
        self.raw_mentions = []
        self.mention_everyone = False
        self.reference = None
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
    """Claim and start a user-install /maxwell turn. True if this was ours."""
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
    prompt, attachments = parse_user_install_command(interaction)
    if not str(prompt).strip():
        await _ephemeral(interaction, "Prompt is required.")
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
    message = UserInstallMessageAdapter(interaction, str(prompt).strip(), attachments)
    spawn = getattr(bot, "_spawn_detached", None)
    on_message = getattr(bot, "on_message", None)
    if callable(spawn) and callable(on_message):
        spawn(on_message(message))
    elif callable(on_message):
        await on_message(message)
    return True
