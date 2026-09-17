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


def _reply_embed(text: str) -> discord.Embed:
    # UserInstallSession already chunks normal replies below Discord's message
    # limit, so one chunk comfortably fits an embed description (4096 chars).
    embed = discord.Embed(description=text[:4096])
    return embed


def install_maxwell_embed_output(bot: Any) -> None:
    """Render textual /maxwell follow-ups as Discord embeds instead of content."""

    del bot
    global _INSTALLED, _ORIGINAL_SEND
    if _INSTALLED:
        return

    current = ui.UserInstallSession.send
    if getattr(current, "_maxwell_embed_output", False):
        _INSTALLED = True
        return
    _ORIGINAL_SEND = current

    async def embedded_send(
        self: Any,
        content: str | None = None,
        file: Any = None,
        **kwargs: Any,
    ) -> Any:
        text = None if content is None else str(content)
        has_explicit_embed = kwargs.get("embed") is not None or bool(kwargs.get("embeds"))
        if (
            text
            and _is_maxwell_slash(getattr(self, "interaction", None))
            and not has_explicit_embed
        ):
            kwargs["embed"] = _reply_embed(text)
            content = None
        return await current(self, content=content, file=file, **kwargs)

    embedded_send._maxwell_embed_output = True  # type: ignore[attr-defined]
    ui.UserInstallSession.send = embedded_send
    _INSTALLED = True


__all__ = ["install_maxwell_embed_output"]
