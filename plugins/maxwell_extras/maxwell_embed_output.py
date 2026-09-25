"""Embed rendering for the /maxwell personal-app command.

Discord personal-app replies used to be plain webhook text even though the
transport already supports embeds. Keep context-menu actions alone and only
upgrade the slash /maxwell surface requested by the user.
"""

from __future__ import annotations

from typing import Any

import discord

import user_install as ui

_INSTALLED = False
_ORIGINAL_SEND = None
_EMBED_COLOR = 0x5865F2


def _is_maxwell_slash(interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    name = str(data.get("name") or "")
    command_type = data.get("type")
    command_type = getattr(command_type, "value", command_type)
    try:
        command_type = int(command_type) if command_type is not None else 1
    except (TypeError, ValueError):
        command_type = 1
    return name == ui.USER_INSTALL_COMMAND_NAME and command_type == 1


def _bot_identity(bot: Any) -> tuple[str, str | None]:
    user = getattr(bot, "user", None)
    name = (
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or "Maxwell"
    )
    avatar = getattr(user, "display_avatar", None)
    avatar_url = getattr(avatar, "url", None)
    if avatar_url is None:
        avatar = getattr(user, "avatar", None)
        avatar_url = getattr(avatar, "url", None)
    return str(name), str(avatar_url) if avatar_url else None


def _reply_embed(text: str, bot: Any = None) -> discord.Embed:
    """Build the automatic branded card used by textual /maxwell answers."""

    # UserInstallSession already chunks normal replies below Discord's message
    # limit, so one chunk comfortably fits an embed description (4096 chars).
    embed = discord.Embed(description=text[:4096], color=_EMBED_COLOR)
    name, avatar_url = _bot_identity(bot)
    if avatar_url:
        embed.set_author(name=name, icon_url=avatar_url)
    else:
        embed.set_author(name=name)
    return embed


def install_maxwell_embed_output(bot: Any) -> None:
    """Render textual /maxwell follow-ups as branded Discord embeds."""

    global _INSTALLED

    def factory(original):
        async def embedded_send(
            self: Any,
            content: str | None = None,
            file: Any = None,
            **kwargs: Any,
        ) -> Any:
            text = None if content is None else str(content)
            has_explicit_embed = kwargs.get("embed") is not None or bool(
                kwargs.get("embeds")
            )
            if (
                text
                and _is_maxwell_slash(getattr(self, "interaction", None))
                and not has_explicit_embed
            ):
                kwargs["embed"] = _reply_embed(text, bot)
                content = None
            return await original(self, content=content, file=file, **kwargs)

        embedded_send._maxwell_embed_output = True  # type: ignore[attr-defined]
        return embedded_send

    ui.wrap_session_send(factory, name="maxwell_embed", priority=10)
    _INSTALLED = True


__all__ = ["install_maxwell_embed_output"]
