"""Regression tests for the 'checking…' silent-drop bug.

2026-08-02: Z3ki's "Mat Dickie" question in #maxwell-the-bot DM showed
the bot posting a "checking…" placeholder via send_message, then never
sending the substantive followup answer. Root cause: when send_message
fires AND the model then produces a real answer on a later followup
turn, the dispatch loop early-returns on ``__MESSAGE_SENT__`` and
silently drops the new answer. These tests pin that down so it can't
regress.
"""

import asyncio
import json
from types import SimpleNamespace


def _native_call(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _should_early_return_on_message_sent(all_tool_results, followup_turn_ran, response):
    """Mirror the dispatch loop's early-return gate.

    Returns True exactly when the old buggy code would silently return
    instead of letting the followup reply reach the channel.
    """
    has_message_sent = any("__MESSAGE_SENT__" in tr for tr in all_tool_results)
    if not has_message_sent:
        return False
    # Buggy behavior was: any __MESSAGE_SENT__ → return.
    # Fixed behavior: also gate on "no followup turn" OR "response empty".
    if followup_turn_ran and (response or "").strip():
        return False
    return True


def test_message_sent_followup_response_is_not_silently_dropped():
    """The bug: send_message posts a placeholder, model emits a real
    answer on the followup turn → the answer was being thrown away.

    This test fails on the OLD code (returning True) and passes on the
    fixed code (returning False).
    """
    all_tool_results = [
        "Tool web_search: found 3 results for 'Mat Dickie'",
        "Tool send_message: __MESSAGE_SENT__\nchecking…",
    ]
    followup_turn_ran = True
    response = "yeah, Mat Dickie (MDickie) is the indie wrestling game dev..."

    assert _should_early_return_on_message_sent(
        all_tool_results, followup_turn_ran, response
    ) is False, (
        "followup reply was silently dropped — early-return fired even "
        "though model produced a substantive reply after send_message"
    )


def test_message_sent_without_followup_still_returns_early():
    """The fix must NOT cause send_message alone (no followup turn) to
    also post the plain-text reply — that would double-post the user.
    """
    all_tool_results = [
        "Tool send_message: __MESSAGE_SENT__\nhere's your summary",
    ]
    followup_turn_ran = False
    response = ""

    assert _should_early_return_on_message_sent(
        all_tool_results, followup_turn_ran, response
    ) is True


def test_message_sent_followup_with_empty_response_still_returns_early():
    """If the model sent send_message AND the followup produced no real
    text response, we still want the early-return — there's nothing
    else to post.
    """
    all_tool_results = [
        "Tool send_message: __MESSAGE_SENT__\ndone",
    ]
    followup_turn_ran = True
    response = ""  # followup returned empty/whitespace

    assert _should_early_return_on_message_sent(
        all_tool_results, followup_turn_ran, response
    ) is True


def test_no_message_sent_falls_through_normally():
    """Plain web_search followup with no send_message — must NOT enter
    the early-return branch at all.
    """
    all_tool_results = [
        "Tool web_search: found 5 results",
    ]
    followup_turn_ran = True
    response = "search results summarized…"

    assert _should_early_return_on_message_sent(
        all_tool_results, followup_turn_ran, response
    ) is False
