"""Suppress redundant prose around tools that already produced the Discord result.

Native tool-call turns may contain assistant text next to the tool_calls payload.
That text is usually pre-action chatter ("here's...", "done", etc.), not a second
answer. Keep it in model history, but do not surface it after the tool executes.

Maxwell also already had a short-followup guard for send_message. Extend its
"already sent" signal to file/media/poll/rich-message delivery so a tool can
finish the turn cleanly while still allowing substantial follow-up text.
"""

from __future__ import annotations

from types import MethodType
from typing import Any



def tool_result_is_visible(result: Any) -> bool:
    """Whether a tool result means the user already received a visible effect."""

    from maxwell_core.tools.results import _turn_sent_message

    return _turn_sent_message([str(result or "")])


def tool_batch_has_visible_output(results: list[str] | None) -> bool:
    return any(tool_result_is_visible(result) for result in (results or []))


def _native_calls_from_wrapper_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if "native_tool_calls" in kwargs:
        return kwargs.get("native_tool_calls")
    # Bound _dispatch_tool_calls receives message, response, then optional calls.
    return args[2] if len(args) >= 3 else None


def install_visible_output_guard(bot: Any) -> None:
    """Install duplicate-output suppression once on the live Maxwell bot."""

    if getattr(bot, "_maxwell_visible_output_guard_installed", False):
        return

    original_dispatch = getattr(bot, "_dispatch_tool_calls", None)
    if callable(original_dispatch) and not getattr(
        original_dispatch, "_maxwell_visible_output_guard_wrapped", False
    ):

        async def dispatch_wrapper(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
            native_calls = _native_calls_from_wrapper_args(args, kwargs)
            result = await original_dispatch(*args, **kwargs)
            if not native_calls or not isinstance(result, tuple) or len(result) < 2:
                return result

            parts = list(result)
            tool_results = parts[1] if isinstance(parts[1], list) else []
            if tool_results:
                # The assistant text emitted beside tool_calls is not a final
                # user-facing answer. The executed tool (or its follow-up turn)
                # owns the visible result.
                parts[0] = ""
            return tuple(parts)

        dispatch_wrapper._maxwell_visible_output_guard_wrapped = True  # type: ignore[attr-defined]
        bot._dispatch_tool_calls = MethodType(dispatch_wrapper, bot)

    # _apply_send_followup_guard() asks this module-global helper whether the
    # turn has already sent something. Broaden that signal to visible file,
    # media, TTS, poll, and rich-message tools so short redundant prose is cut.
    handle_message = getattr(bot, "_handle_message", None)
    func = getattr(handle_message, "__func__", handle_message)
    namespace = getattr(func, "__globals__", None)
    if isinstance(namespace, dict):
        original_turn_sent = namespace.get("_turn_sent_message")
        if callable(original_turn_sent) and not getattr(
            original_turn_sent, "_maxwell_visible_output_guard_wrapped", False
        ):

            def turn_sent_wrapper(tool_results: list[str] | None) -> bool:
                return bool(original_turn_sent(tool_results)) or tool_batch_has_visible_output(
                    tool_results
                )

            turn_sent_wrapper._maxwell_visible_output_guard_wrapped = True  # type: ignore[attr-defined]
            namespace["_turn_sent_message"] = turn_sent_wrapper

    bot._maxwell_visible_output_guard_installed = True


__all__ = [
    "install_visible_output_guard",
    "tool_batch_has_visible_output",
    "tool_result_is_visible",
]
