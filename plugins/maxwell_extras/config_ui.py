"""Shared private settings interaction lifecycle."""

from __future__ import annotations

from functools import wraps
from typing import Any

import user_install as ui


async def _send(interaction: Any, text: str) -> None:
    await ui._ephemeral(interaction, str(text)[:1900])


async def _acknowledge(interaction: Any, *, thinking: bool = False) -> None:
    response = getattr(interaction, "response", None)
    done = getattr(response, "is_done", None)
    defer = getattr(response, "defer", None)
    if callable(defer) and not (callable(done) and done()):
        await defer(ephemeral=True, thinking=thinking)


async def _edit_panel(interaction: Any, **payload: Any) -> None:
    response = interaction.response
    done = getattr(response, "is_done", None)
    if callable(done) and done():
        await interaction.edit_original_response(**payload)
    else:
        await response.edit_message(**payload)


def _config_action(*, thinking: bool = False):
    """Acknowledge before I/O and serialize changes to a shared settings panel."""

    def decorate(callback):
        @wraps(callback)
        async def run(self, interaction, *args, **kwargs):
            panel = getattr(self, "panel", self)
            if not await panel.authorized(interaction):
                return
            await _acknowledge(interaction, thinking=thinking)
            async with panel._action_lock:
                if panel.closed or panel.is_finished():
                    await _send(
                        interaction,
                        "This menu is closed or expired. Run `/config` again.",
                    )
                    return
                context = kwargs.pop("context", getattr(self, "_config_context", None))
                if context is not None and context != (panel.scope, panel.selected_key):
                    await _send(
                        interaction,
                        "This control has changed. Use the current settings menu.",
                    )
                    return
                if not await panel.authorized(interaction):
                    return
                await panel._refresh_personal()
                await callback(self, interaction, *args, **kwargs)

        return run

    return decorate
