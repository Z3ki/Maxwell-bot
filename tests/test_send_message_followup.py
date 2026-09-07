"""Regression tests for send_message vs leftover plaintext.

2026-08-02: a "checking…" placeholder via send_message dropped the later
follow-up answer. 2026-08-14: leftover assistant content on the SAME
generation as send_message posted a second Discord reply ping.

2026-09-06: after send_message, a short filler follow-up with no tool
(the model writing "ok" instead of no_response) posted a second Discord
reply. Dispatcher now drops that leftover and ends the turn.
"""

import json

from bot import (
    _apply_send_followup_guard,
    _only_promise_results,
    _should_skip_plaintext_after_send,
    _tool_results_need_followup,
)

_LONG_FOLLOWUP = (
    "yeah, Mat Dickie (MDickie) is the indie wrestling game dev behind "
    "Wrestling Revolution and a pile of other locker-room sims. the Yotta "
    "writeup is a bit stale — the 2024 numbers are the ones to use, and the "
    "dispatch loop lives in bot.py so a follow-up turn is what feeds results "
    "back to the model instead of dropping the answer on the floor."
)
assert len(_LONG_FOLLOWUP) > 200


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
            response=_LONG_FOLLOWUP,
        )
        is False
    )


def test_short_filler_after_send_is_dropped():
    last = []
    all_results = ["Tool send_message: __MESSAGE_SENT__\nchecking…"]
    for text in (
        "ok",
        "done",
        "hope that helps!",
        "let me know if you need anything",
        "got it",
    ):
        assert (
            _should_skip_plaintext_after_send(
                last,
                all_results,
                followup_turn_ran=True,
                response=text,
            )
            is True
        ), text


def test_same_generation_long_leftover_is_still_skipped():
    last = ["Tool send_message: __MESSAGE_SENT__\nhello"]
    assert (
        _should_skip_plaintext_after_send(
            last, last, followup_turn_ran=False, response=_LONG_FOLLOWUP
        )
        is True
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


# ---------------------------------------------------------------------------
# 2026-09-06: after send_message, do not generate again just to close, and
# do not post short leftover prose. Another send_message / real tool in the
# next step still runs. Multi-send in one native batch is covered by
# tests/test_wait_tool.py.
# ---------------------------------------------------------------------------


def test_send_message_alone_does_not_ask_for_another_generation():
    assert not _tool_results_need_followup(
        ["Tool send_message: __MESSAGE_SENT__\nhere's the answer"]
    )


def test_multi_send_in_one_batch_does_not_loop():
    results = [
        "Tool send_message: __MESSAGE_SENT__\nfirst",
        "Tool send_message: __MESSAGE_SENT__\nsecond",
    ]
    assert _tool_results_need_followup(results) is False


def test_guard_drops_short_bare_text_after_send():
    text, stop = _apply_send_followup_guard(True, [], "ok")
    assert stop is True
    assert text == ""


def test_guard_drops_empty_followup_after_send():
    text, stop = _apply_send_followup_guard(True, None, "   ")
    assert stop is True
    assert text == ""


def test_guard_keeps_long_answer_but_stops_the_loop():
    text, stop = _apply_send_followup_guard(True, [], _LONG_FOLLOWUP)
    assert stop is True
    assert text == _LONG_FOLLOWUP


def test_guard_runs_another_send_message():
    pending = [_native_call("send_message", {"content": "part 2"})]
    text, stop = _apply_send_followup_guard(True, pending, "")
    assert stop is False
    assert text == ""


def test_guard_runs_a_real_followup_tool():
    pending = [_native_call("web_search", {"query": "Mat Dickie"})]
    _, stop = _apply_send_followup_guard(True, pending, "searching")
    assert stop is False


def test_guard_lets_no_response_dispatch():
    pending = [_native_call("no_response", {})]
    _, stop = _apply_send_followup_guard(True, pending, "")
    assert stop is False


def test_guard_is_noop_when_nothing_was_sent():
    text, stop = _apply_send_followup_guard(False, [], "ok")
    assert stop is False
    assert text == "ok"
