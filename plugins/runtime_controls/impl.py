"""Tool implementations for the runtime_controls plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

class SleepTool(Tool):
    """Take a sleep window. While sleeping the bot won't dispatch
    LLM turns — the triggering channel gets a 'max is sleeping,
    back in Xm' notice (deduped per user, never a DM). The 2026-07-19
    user directive: the bot kept spamming goodnight/goodbye in chat;
    a real sleep window is the structural fix. Use this when the
    conversation is genuinely winding down — not as a generic
    goodbye."""
    tool_name = 'sleep'
    returns_result = False
    ends_turn = True


    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Sleep 1-60 minutes (default 30). While asleep, LLM turns are skipped "
            "and the triggering channel gets one sleeping notice. Use only "
            "at a real end-of-conversation, not as a goodbye. Calling again resets "
            "the window. Params: duration_minutes."
        )

    async def execute(
        self,
        message: Message,
        duration_minutes: int | str = 30,
        **kwargs,
    ) -> str:
        # Defensive parse — the model may emit a string.
        try:
            n = int(duration_minutes)
        except (TypeError, ValueError):
            n = 30
        if n < 1:
            n = 1
        if n > 60:
            n = 60
        if self.bot is None:
            return "Error: bot not attached, cannot sleep"
        result = self.bot.set_sleep(n)
        if asyncio.iscoroutine(result):
            return await result
        return result

class ClearSleepTool(Tool):
    """Cancel an active sleep window. Idempotent — safe to call when
    not sleeping. Use when the bot decided to sleep but the user
    immediately needs a reply."""
    tool_name = 'clear_sleep'
    returns_result = True
    ends_turn = False


    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Cancel the active sleep window and wake immediately. "
            "Use only if you just slept and the user still needs you."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        if self.bot is None:
            return "Error: bot not attached"
        result = self.bot.clear_sleep()
        if asyncio.iscoroutine(result):
            return await result
        return result

class WaitTool(Tool):
    """Pause before later actions in this batch, for at most 10 seconds.

    This does not schedule a model follow-up or reopen an ended turn.
    send_message still ends the batch; put any wait before it.
    sleep suspends the bot for minutes and ends dispatch.
    """
    tool_name = 'wait'
    returns_result = False
    ends_turn = False


    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Pause this tool batch (`wait`) for `seconds` (float, default 2, max 10). "
            "Put wait before the final send_message; it cannot reopen an ended turn. "
            "Wait does not schedule another model turn. If someone is typing, pause briefly. "
            "Distinct from sleep (minutes, ends dispatch)."
        )

    async def execute(
        self,
        message: Message,
        seconds: float | str = 2.0,
        **kwargs,
    ) -> str:
        try:
            n = float(seconds)
        except (TypeError, ValueError):
            n = 2.0
        if n < 0:
            n = 0.0
        if n > 10:
            # Hard cap. The model can't override this; longer pauses belong
            # in `sleep`. The error is visible to the model so it can
            # adjust instead of silently truncating.
            return "Error: wait duration capped at 10 seconds. Use `sleep` for longer pauses."
        await asyncio.sleep(n)
        return f"Waited {n:.1f}s"

class MoreToolsTool(Tool):
    """No-op leftover. The full catalog is attached on every turn.

    Older prompts told the model to call this to unlock tools mid-turn.
    That hid tools like hd_image behind a hop, so a photo request that
    started without the verb "generate" got the from-scratch generator
    instead. Kept registered so a stale call does not error.
    """
    tool_name = 'more_tools'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "No-op. You already have every tool this turn. Call the one you "
            "need directly — this does not unlock anything."
        )

    async def execute(self, message: Message, need: str | None = None, **kwargs) -> str:
        logger.info("more_tools: no-op (catalog is already full, need=%r)", str(need or "")[:120])
        return (
            "You already have the full tool catalog this turn. "
            "Call the tool you need directly — more_tools does not unlock anything."
        )
