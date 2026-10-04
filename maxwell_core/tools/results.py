"""Tool result contracts and policies for follow-up generation and delivery."""

import re

from tool_schemas import (
    RESULT_TOOL_NAMES,
    returns_result as tool_schemas_returns_result,
)


FOLLOWUP_TOOL_NAMES = RESULT_TOOL_NAMES


_PROMISE_VERB = (
    r"build|make|create|check|look|get|do|write|set|start|spin|generate|"
    r"run|fix|add|update|deploy|test|grab|pull|draft|put"
)


_PROMISE_RE = re.compile(
    # Bare acknowledgements.
    r"\b(?:on it|working on it|checking(?: that)?(?: now)?|let me check|"
    r"give me a sec|one sec|hold on|two secs|"
    # "I'll set that up", "gonna spin it up", "let me go build this" — the
    # intent phrase, then up to two filler words, then a doing-verb.
    rf"(?:i'?ll|i will|gonna|going to|let me|lemme)\s+(?:\w+\s+){{0,2}}?(?:{_PROMISE_VERB})|"
    # Present progressive with no result yet.
    r"(?:building|making|creating|generating|starting|setting|spinning|"
    r"drafting|putting)\b[^.!?]{0,40}\b(?:it|that|this|now|up)|"
    r"looking into (?:it|that|this))\b",
    re.I,
)


_PROMISE_MAX_CHARS = 200


_SHORT_FOLLOWUP_AFTER_SEND_CHARS = 200


def _promises_followup_work(result: str) -> bool:
    """True when a send_message result is a bare "on it…" promise.

    The turn must loop back so the announced work actually happens. Anything
    long enough to be a substantive reply is treated as a real answer.
    """
    idx = result.find("__MESSAGE_SENT__")
    if idx < 0:
        return False
    sent = result[idx + len("__MESSAGE_SENT__") :].strip()
    if not sent or len(sent) > _PROMISE_MAX_CHARS:
        return False
    return bool(_PROMISE_RE.search(sent))


def _plugin_result_needs_followup(result: str) -> bool:
    """Whether a tool result came from a plugin tool that returns output.

    Plugin tool names are discovered at import time, so they cannot be in the
    static FOLLOWUP_TOOL_NAMES set. Without this check a plugin that looked
    something up had its output collected and then discarded when the dispatch
    loop broke — the model never saw what it asked for.
    """
    if not result.startswith("Tool "):
        return False
    head = result[5:].split(":", 1)[0].strip()
    return bool(head) and tool_schemas_returns_result(head)


def _tool_results_need_followup(tool_results: list[str]) -> bool:
    # First pass: does the batch contain anything that needs a model turn
    # (a follow-up tool result, or an error)? If yes, we ALWAYS loop back,
    # even if the batch also contains a terminal send_message. Otherwise a
    # send_message + shell pair in one batch would short-circuit, and the
    # model would never get to react to the shell output.
    has_followup_signal = False
    for result in tool_results:
        # Check for error prefixes, not just the substring "Error" anywhere
        # (prevents false positives like "Error handling in Python" search results)
        if (
            result.startswith(("Error:", "Error ", "Tool no_response: Error:"))
            or "\nError:" in result
        ):
            return True
        if any(result.startswith(f"Tool {name}:") for name in FOLLOWUP_TOOL_NAMES):
            has_followup_signal = True
        elif _plugin_result_needs_followup(result):
            has_followup_signal = True
    if has_followup_signal:
        return True

    # Second pass: no follow-up tool in the batch, so a terminal action
    # (send_message or explicit no_response) genuinely ends the turn.
    for result in tool_results:
        if "__MESSAGE_SENT__" in result:
            # A send_message that only promises future work is NOT terminal.
            # The protocol tells the model not to ack, but it still emits
            # "on it…" alone and plans to act "next turn". There is no next
            # turn: send_message is not in RESULT_TOOL_NAMES, so with no
            # other tool in the batch this returns False, the dispatch loop
            # breaks, and the promise is all the user ever gets. Loop back
            # once so the model actually runs the thing it just announced.
            if _promises_followup_work(result):
                return True
            return False
        if result.startswith("Tool no_response:") and "__NO_RESPONSE__" in result:
            return False

    return False


def _only_promise_results(tool_results: list[str]) -> bool:
    """True when the batch is nothing but ack-only send_message promises.

    Used to bound the extra loop: a batch that also ran a real tool already
    loops via FOLLOWUP_TOOL_NAMES and must not consume the promise budget.
    """
    saw_promise = False
    for result in tool_results:
        if "__MESSAGE_SENT__" in result:
            if not _promises_followup_work(result):
                return False
            saw_promise = True
            continue
        if result.strip():
            return False
    return saw_promise


_VISIBLE_RESULT_MARKERS = (
    "__MESSAGE_SENT__",
    "__FILE_SENT__",
    "__MEDIA_SENT__",
    "__MEME_SENT__",
    "__POLL_SENT__",
)


def _turn_sent_message(tool_results: list[str] | None) -> bool:
    """True when a tool already delivered the user-visible result this turn."""
    for tr in tool_results or []:
        text = str(tr or "")
        if any(marker in text for marker in _VISIBLE_RESULT_MARKERS):
            return True
        lowered = text.lower()
        if "tool send_rich_message:" in lowered and not lowered.startswith(
            ("tool send_rich_message: error",)
        ):
            if "error" not in lowered.split(":", 1)[-1][:12]:
                return True
        if (
            "tool create_poll:" in lowered
            and "error" not in lowered.split(":", 1)[-1][:12]
        ):
            return True
    return False


def _is_short_plaintext_followup(response: str) -> bool:
    text = (response or "").strip()
    return bool(text) and len(text) <= _SHORT_FOLLOWUP_AFTER_SEND_CHARS


def _apply_send_followup_guard(
    already_sent: bool,
    pending_tool_calls: list | None,
    response,
) -> tuple[str, bool]:
    """End the turn after send_message when the next output has no tool.

    Returns ``(visible_response, stop_loop)``. Another ``send_message`` or
    any real tool still runs. Bare short/empty text is cleared so it cannot
    post as a second reply. Bare long text is kept (placeholder then the
    real answer) but the loop still stops — do not generate again hoping
    for ``no_response``.
    """
    text = response if isinstance(response, str) else str(response or "")
    if not already_sent or pending_tool_calls:
        return text, False
    stripped = text.strip()
    if not stripped or _is_short_plaintext_followup(text):
        return "", True
    return stripped, True


def _should_skip_plaintext_after_send(
    last_tool_results: list[str],
    all_tool_results: list[str],
    followup_turn_ran: bool,
    response: str,
) -> bool:
    """Skip leftover assistant text when send_message already delivered.

    Same-generation leftover (tool_calls + content) must not post as a
    second Discord reply. A later follow-up turn with real text and no
    new send_message still posts, so a "checking…" placeholder can be
    followed by the actual answer — unless that leftover is short filler
    the model wrote instead of ``no_response``.
    """
    last = last_tool_results or []
    if _turn_sent_message(last):
        return True
    if not _turn_sent_message(all_tool_results):
        return False
    text = (response or "").strip()
    if not text:
        return True
    if followup_turn_ran and not _is_short_plaintext_followup(text):
        return False
    return True
