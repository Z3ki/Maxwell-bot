"""Remove the legacy `,confirm` UX without weakening the taint gate.

A destructive tool call that follows fetched/web content remains blocked. The
manual one-shot confirmation command is gone; the user can issue the intended
action again in a fresh message, or explicitly disable the taint gate in the
install configuration.
"""

from __future__ import annotations

from types import MethodType
from typing import Any

_INSTALLED_ATTR = "_maxwell_taint_gate_cleanup_installed"


def _command_name(bot: Any, message: Any) -> str:
    prefix = str(getattr(bot, "command_prefix", None) or ",")
    raw = str(getattr(message, "content", "") or "")
    if not raw.startswith(prefix):
        return ""
    rest = raw[len(prefix) :].strip()
    return rest.split(maxsplit=1)[0].lower() if rest else ""


def _taint_gate_enabled(bot: Any) -> bool:
    config = getattr(bot, "config", None)
    return not bool(getattr(config, "DISABLE_TAINT_GATE", False))


def _is_tainted(bot: Any, message: Any) -> bool:
    check = getattr(bot, "is_message_tainted", None)
    if not callable(check):
        return False
    try:
        return bool(check(message))
    except Exception:
        # If the taint state itself cannot be checked, fail closed for
        # destructive tools rather than silently bypassing the protection.
        return True


def install_taint_gate_cleanup(bot: Any) -> None:
    """Remove `,confirm` and keep destructive tainted turns fail-closed."""

    if getattr(bot, _INSTALLED_ATTR, False):
        return

    original_handle_command = getattr(bot, "_handle_command", None)
    if callable(original_handle_command):

        async def handle_command(self_obj: Any, message: Any) -> Any:
            # Treat the old command as removed. Returning a handled result keeps
            # it from becoming an AI chat turn and avoids generating any legacy
            # confirmation token or user-visible status.
            if _command_name(self_obj, message) == "confirm":
                return None
            return await original_handle_command(message)

        handle_command._maxwell_taint_gate_cleanup = True  # type: ignore[attr-defined]
        bot._handle_command = MethodType(handle_command, bot)

    original_execute = getattr(bot, "_execute_tool_by_name", None)
    if callable(original_execute):

        async def execute_tool(
            self_obj: Any,
            message: Any,
            name: str,
            params: dict,
            *,
            disabled: set,
            compatible: set,
        ) -> str:
            tool = (getattr(self_obj, "tools", None) or {}).get(name)
            if (
                bool(getattr(tool, "is_destructive", False))
                and _taint_gate_enabled(self_obj)
                and _is_tainted(self_obj, message)
            ):
                return (
                    f"Tool {name}: refused: destructive tools are blocked on a turn "
                    "that read fetched/web content. Send a fresh message with the exact "
                    "action you want to run. Set DISABLE_TAINT_GATE=true only if you "
                    "intentionally want to disable this protection."
                )
            return await original_execute(
                message,
                name,
                params,
                disabled=disabled,
                compatible=compatible,
            )

        execute_tool._maxwell_taint_gate_cleanup = True  # type: ignore[attr-defined]
        bot._execute_tool_by_name = MethodType(execute_tool, bot)

    # The old implementation kept one-shot tokens here. Clear any stale state
    # left over after a hot plugin reload; new calls never create it again.
    if hasattr(bot, "_destructive_confirm"):
        bot._destructive_confirm = {}

    setattr(bot, _INSTALLED_ATTR, True)


__all__ = ["install_taint_gate_cleanup"]
