"""Tool implementations for the personality plugin.

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

class UpdateBasePersonalityTool(Tool):
    """Rewrite the global base_personality that ships in every prompt.

    This is what the model reads as its tone / do-don'ts / identity safety
    section. The MAXWELL_BASE_KNOWLEDGE block (identity, slang, voice, memes)
    is ALWAYS-ON and lives in code — it is NOT editable through this tool.
    This tool only rewrites the per-runtime personality paragraph that lives
    in bot_control.json under `base_personality`.
    """
    tool_name = 'update_base_personality'
    returns_result = True
    ends_turn = False
    requires_admin = True


    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Rewrite global base_personality (tone/do-don'ts in every prompt). "
            "Base Knowledge in code is not editable. Params: text (100-2000 chars)."
        )

    async def execute(
        self,
        message: Message,
        text: str | None = None,
        **kwargs,
    ) -> str:
        if not _caller_is_admin(self.bot, message):
            return "Error: restricted to admins."
        if not text or not str(text).strip():
            return "Error: 'text' is required and cannot be empty."
        text = str(text).strip()
        # Soft cap: personality blocks over 4000 chars are usually a sign
        # someone pasted a whole essay. Reject and ask for a tighter version.
        if len(text) > 4000:
            return (
                f"Error: text is {len(text)} chars; the soft cap is 4000. "
                "Tighten the wording — a long personality is a context-killer, "
                "and the bot doesn't read past the most recent instructions anyway."
            )
        if len(text) < 20:
            return (
                f"Error: text is {len(text)} chars; too short to be a useful "
                "personality. Aim for at least 100-300 chars of voice/do-don'ts."
            )

        try:
            control = dict(self.bot._control)
            control["base_personality"] = text
            self.bot._control = control
            import asyncio
            from pathlib import Path

            await asyncio.to_thread(
                _atomic_json_write_sync,
                Path(self.bot.config.DATA_DIR) / "bot_control.json",
                control,
            )
        except Exception as e:
            return f"Error: failed to persist base_personality: {e}"
        return (
            f"base_personality updated. {len(text)} chars written to "
            "bot_control.json. The change is live on the next turn — no "
            "restart needed. MAXWELL_BASE_KNOWLEDGE (in code) was NOT "
            "touched; only the per-runtime personality paragraph was rewritten."
        )

class UpdateServerPromptTool(Tool):
    """Rewrite the per-server custom prompt (same as `,prompt <text>`).

    Same effect as the `,prompt <text>` command but invokable from
    inside an LLM turn — Maxwell can edit its own per-server instructions
    when it has a reason. Pass server_id (numeric snowflake) or pass 'DM'
    for the DM default. Pass empty text to clear the per-server prompt.
    """
    tool_name = 'update_server_prompt'
    returns_result = True
    ends_turn = False
    requires_admin = True


    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Rewrite or clear the per-server custom prompt (same as `,prompt`). "
            "Params: server_id (snowflake or 'DM'), text (empty or '__CLEAR__' "
            "to clear)."
        )

    async def execute(
        self,
        message: Message,
        server_id: str | None = None,
        text: str | None = None,
        **kwargs,
    ) -> str:
        if not _caller_is_admin(self.bot, message):
            return "Error: restricted to admins."
        if not server_id or not str(server_id).strip():
            return "Error: 'server_id' is required (numeric snowflake or 'DM')."
        server_id = str(server_id).strip()
        # Soft cap mirrors the personality cap.
        if text is not None and len(str(text)) > 4000:
            return (
                f"Error: text is {len(text)} chars; the soft cap is 4000. "
                "Per-server prompts over 4000 chars are context-killers."
            )

        text_str = "" if text is None else str(text)
        cleared = text_str.strip() in ("", "__CLEAR__")

        try:
            if cleared:
                self.bot.memory.clear_server_prompt(server_id)
                return (
                    f"Cleared per-server prompt for server_id={server_id}. "
                    "The bot will fall back to base_personality + "
                    "MAXWELL_BASE_KNOWLEDGE in that server from now on."
                )
            self.bot.memory.set_server_prompt(server_id, text_str)
        except Exception as e:
            return f"Error: failed to persist server prompt: {e}"
        return (
            f"Server prompt updated for server_id={server_id}. "
            f"{len(text_str)} chars written. The change is live on the next "
            f"turn in that server — no restart needed."
        )
