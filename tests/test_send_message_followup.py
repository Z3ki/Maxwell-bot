"""Regression tests for send_message vs leftover plaintext.

2026-08-02: a "checking…" placeholder via send_message dropped the later
follow-up answer. 2026-08-14: leftover assistant content on the SAME
generation as send_message posted a second Discord reply ping.
"""

import json

from bot import (
    _only_promise_results,
    _should_skip_plaintext_after_send,
    _tool_results_need_followup,
)


def _native_call(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_same_generation_leftover_does_not_post_second_reply():
    last = ["Tool send_message: __MESSAGE_SENT__\nquick research dump: ..."]
    all_results = [
        "Tool web_search: found 3 results",
        last[0],
    ]
    assert (
        _should_skip_plaintext_after_send(
            last,
            all_results,
            followup_turn_ran=True,
            response="The Yotta article is slightly stale...",
        )
        is True
    )


def test_message_sent_followup_response_is_not_silently_dropped():
    last = []  # follow-up turn had no new send_message
    all_results = [
        "Tool web_search: found 3 results for 'Mat Dickie'",
        "Tool send_message: __MESSAGE_SENT__\nchecking…",
    ]
    assert (
        _should_skip_plaintext_after_send(
            last,
            all_results,
            followup_turn_ran=True,
            response="yeah, Mat Dickie (MDickie) is the indie wrestling game dev...",
        )
        is False
    )


def test_message_sent_without_followup_still_returns_early():
    last = ["Tool send_message: __MESSAGE_SENT__\nhere's your summary"]
    assert (
        _should_skip_plaintext_after_send(
            last, last, followup_turn_ran=False, response=""
        )
        is True
    )


def test_message_sent_followup_with_empty_response_still_returns_early():
    last = []
    all_results = ["Tool send_message: __MESSAGE_SENT__\ndone"]
    assert (
        _should_skip_plaintext_after_send(
            last, all_results, followup_turn_ran=True, response=""
        )
        is True
    )


def test_no_message_sent_falls_through_normally():
    last = ["Tool web_search: found 5 results"]
    assert (
        _should_skip_plaintext_after_send(
            last,
            last,
            followup_turn_ran=True,
            response="search results summarized…",
        )
        is False
    )


# ---------------------------------------------------------------------------
# 2026-08-30: "on it" / "working on it" that never actually did the work.
#
# FULL_TOOL_PROTOCOL invites a fast acknowledgement alongside a slow tool, but
# the model routinely emitted the ack as the ONLY tool call and planned to act
# "next turn". send_message is not in RESULT_TOOL_NAMES, so an ack-only batch
# made _tool_results_need_followup() return False, the dispatch loop broke, and
# the promise was the entire response. These lock in the loop-back.
# ---------------------------------------------------------------------------


def test_ack_only_send_message_loops_back_so_work_actually_runs():
    for text in (
        "on it...",
        "working on it…",
        "checking that now",
        "one sec",
        "i'll build that for you",
        "gonna set that up now",
        "going to run that",
        "lemme check real quick",
        "let me go grab that",
        "building it now",
        "generating that image now",
        "drafting it up now",
        "looking into it",
        "two secs",
    ):
        results = [f"Tool send_message: __MESSAGE_SENT__\n{text}"]
        assert _tool_results_need_followup(results) is True, text
        assert _only_promise_results(results) is True, text


def test_real_answer_mentioning_work_stays_terminal():
    # A substantive reply must NOT re-generate, even though it contains
    # "working on". Length is the discriminator.
    answer = (
        "yeah I've been working on that codebase for a while — the dispatch "
        "loop lives in bot.py and the tool contract is stamped in tool_schemas, "
        "so the follow-up turn is what feeds results back to the model. "
        "the short version is that it loops until a terminal tool lands."
    )
    results = [f"Tool send_message: __MESSAGE_SENT__\n{answer}"]
    assert _tool_results_need_followup(results) is False
    assert _only_promise_results(results) is False


def test_ordinary_short_reply_is_not_treated_as_a_promise():
    for text in (
        "yeah",
        "lol",
        "done",
        "nope, that's wrong",
        "42",
        "that's built already",
        "the build is done and deployed",
        "created: https://example.com",
        "yeah I set that up last week",
        "done — live at https://x.dev",
    ):
        results = [f"Tool send_message: __MESSAGE_SENT__\n{text}"]
        assert _tool_results_need_followup(results) is False, text
        assert _only_promise_results(results) is False, text


def test_ack_plus_real_tool_does_not_consume_the_promise_budget():
    # This batch already loops via FOLLOWUP_TOOL_NAMES (create_site), so it
    # must not be classified as ack-only — otherwise the one-shot promise
    # budget would be spent on a turn that was already going to loop.
    results = [
        "Tool send_message: __MESSAGE_SENT__\non it...",
        "Tool create_site: live at https://example.com",
    ]
    assert _tool_results_need_followup(results) is True
    assert _only_promise_results(results) is False


def test_no_response_stays_terminal():
    results = ["Tool no_response: __NO_RESPONSE__"]
    assert _tool_results_need_followup(results) is False
    assert _only_promise_results(results) is False


def test_error_still_forces_followup():
    assert _tool_results_need_followup(["Tool shell: Error - boom"]) is True
