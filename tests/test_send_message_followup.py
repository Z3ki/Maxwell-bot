"""Regression tests for send_message vs leftover plaintext.

2026-08-02: a "checking…" placeholder via send_message dropped the later
follow-up answer. 2026-08-14: leftover assistant content on the SAME
generation as send_message posted a second Discord reply ping.
"""

import json

from bot import _should_skip_plaintext_after_send


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
