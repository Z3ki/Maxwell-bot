"""Tool implementations for the diagnostics plugin.

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

class UsageTool(Tool):
    """Read the requesting user's current Maxwell message allowance."""

    tool_name = 'usage'
    returns_result = True
    ends_turn = False
    side_effects = False

    def get_description(self):
        return (
            "Check the requesting user's current Maxwell message usage. No params. "
            "Use when they ask about their usage, remaining allowance, or reset time. "
            "Returns percentages used and remaining in the rolling window, or "
            "whether messages are unlimited. Only checks the user who sent the "
            "request; cannot look up other users or provider accounts."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        from usage_commands import usage_text_for

        author = getattr(message, "author", None) or getattr(message, "user", None)
        user_id = str(getattr(author, "id", "") or "")
        if not user_id:
            return "Message allowance is unavailable: the requesting user is unknown."
        return await asyncio.to_thread(usage_text_for, self.bot, user_id)


class ReportTool(Tool):
    """DM the configured owner with a report or error."""
    tool_name = 'report'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "DM the owner with a report. Use when something is actually broken, "
            "a user asks you to escalate, or the owner needs to know. Do not spam "
            "it for banter. Params: what (required, short summary), details "
            "(optional: who/where/what happened), kind (report|error|info, default report)."
        )

    async def execute(
        self,
        message: Message,
        what: str | None = None,
        details: str | None = None,
        kind: str = "report",
        **kwargs,
    ) -> str:
        return await notify_owner(
            self.bot,
            kind=kind,
            title=str(what or "").strip(),
            details=str(details or "").strip(),
            message=message,
        )

class DebugTool(Tool):
    """Report last-call TTFT, TPS, and token counts."""
    tool_name = 'debug'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Latency debug: last LLM call TTFT (time to first token), TPS "
            "(tokens/sec), tokens in/out, endpoint, and recent averages. "
            "No params. Use when asked how fast/slow generation is."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        return collect_debug_stats(self.bot, channel_id or None)
