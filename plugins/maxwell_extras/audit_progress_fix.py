"""Capture the progress message before ToolProgress.transition_to_final clears it."""

from __future__ import annotations

import contextlib
from typing import Any

from .audit_ui import _MESSAGE_BOTS, _attach_trace, _mid


def install_progress_audit_fix(bot: Any) -> None:
    try:
        from tool_progress import ToolProgress
    except Exception:
        return
    current = ToolProgress.transition_to_final
    if getattr(current, "_maxwell_progress_capture_fix", False):
        return

    async def fixed(self: Any, content: str) -> bool:
        inbound = getattr(self, "_msg", None)
        posted = getattr(self, "posted", None)
        audit_bot = _MESSAGE_BOTS.get(_mid(inbound)) or bot
        ok = await current(self, content)
        if ok and posted is not None and await audit_bot._maxwell_tool_audit_store.has_live(_mid(inbound)):
            with contextlib.suppress(Exception):
                await _attach_trace(audit_bot, posted, inbound)
        return ok

    fixed._maxwell_progress_capture_fix = True  # type: ignore[attr-defined]
    ToolProgress.transition_to_final = fixed
