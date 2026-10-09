"""Prompt cleanup preserves source material, intentional repeats and failures."""

import copy

from maxwell_core.prompts.app_defaults import LEGACY_AUTO_WEB, LEGACY_BRIEF_REPLY
from maxwell_core.prompts.history import (
    canonical_history,
    compact_media_annotations,
    history_text,
)

STAMP = "2026-10-09T02:45:07Z"
URL = "https://cdn.discordapp.com/attachments/1/2/sample.mp3?ex=abc&is=def&hm=123&"


def test_legacy_app_history_keeps_raw_request_and_one_timestamp_source():
    row = {
        "timestamp": STAMP,
        "content": "[at 2026-10-09 02:45:07 UTC] "
        + LEGACY_AUTO_WEB
        + "\n"
        + LEGACY_BRIEF_REPLY
        + "\n\nUser request:\nClone this and say hi",
    }
    before = copy.deepcopy(row)
    assert history_text(row) == "Clone this and say hi"
    assert row == before


def test_custom_user_instructions_and_quoted_harness_examples_are_preserved():
    text = "My custom instructions\n\nUser request:\nDo this"
    assert history_text({"content": text}) == text
    example = LEGACY_AUTO_WEB + "\n\nUser request:\nexample"
    assert history_text({"content": example, "prompt_format_version": 2}) == example
    wrapped = {
        "content": LEGACY_AUTO_WEB
        + "\n"
        + LEGACY_BRIEF_REPLY
        + "\n\nUser request:\n"
        + example
    }
    cleaned = canonical_history([wrapped], "1", {"Maxwell"})
    assert history_text(cleaned[0]) == example  # Migration happens once only.


def test_nonmatching_timestamps_are_not_deleted_from_user_content():
    text = "[at 2026-10-09 02:00:00 UTC] This quote matters"
    assert history_text({"content": text, "timestamp": STAMP}) == text


def test_repeated_media_urls_keep_complete_signed_url_and_metadata_once():
    text = f"Clone {URL}\n[attachments: sample.mp3 (audio/mpeg) {URL}]\n[audio: 18s sample.mp3 {URL}]\n[media URL: audio {URL}]\n1. sample.mp3 (audio/mpeg, new) — {URL}"
    cleaned = compact_media_annotations(text)
    assert cleaned.count(URL) == 1
    assert "18s" in cleaned and "audio/mpeg" in cleaned
    assert cleaned.startswith("Clone " + URL)
    assert compact_media_annotations(cleaned) == cleaned


def test_repeated_urls_in_actual_prose_and_code_are_preserved():
    text = f"Before: {URL}\n```text\nAfter: {URL}\n```"
    assert compact_media_annotations(text) == text


def bot_row(mid, text="sent reply", stamp=STAMP):
    return {
        "message_id": mid,
        "author": "Maxwell",
        "author_id": "1",
        "content": text,
        "timestamp": stamp,
    }


def tool_row(text="sent reply"):
    return {
        "message_id": "tool:9",
        "is_tool": True,
        "tool_name": "send_message",
        "content": 'Called send_message with {"content":"sent reply"} -> __MESSAGE_SENT__\n'
        + text,
        "tool_result": "__MESSAGE_SENT__\n" + text,
        "timestamp": STAMP,
    }


def test_legacy_tool_and_synthetic_echo_collapse_to_actual_discord_message():
    rows = [tool_row(), bot_row("bot_send_message:9"), bot_row("101")]
    before = copy.deepcopy(rows)
    cleaned = canonical_history(rows, "1", {"Maxwell"})
    assert [row["message_id"] for row in cleaned] == ["101"]
    assert cleaned[0]["content"] == "sent reply"
    assert rows == before


def test_same_text_in_two_real_messages_is_not_deduplicated():
    rows = [bot_row("101"), bot_row("102")]
    assert len(canonical_history(rows, "1", {"Maxwell"})) == 2


def test_edits_replace_same_message_id_without_removing_repeated_messages():
    rows = [bot_row("101", "old"), bot_row("102", "new"), bot_row("101", "edited")]
    cleaned = canonical_history(rows, "1", {"Maxwell"})
    assert [(row["message_id"], row["content"]) for row in cleaned] == [
        ("101", "edited"),
        ("102", "new"),
    ]


def test_legacy_second_send_is_preserved_when_only_the_tool_record_exists():
    cleaned = canonical_history(
        [bot_row("101", "first"), tool_row("second")], "1", {"Maxwell"}
    )
    assert [row["content"] for row in cleaned] == ["first", "second"]
    assert not cleaned[1]["is_tool"]
    assert cleaned[1]["author_id"] == "1"


def test_tool_failure_and_same_text_from_other_users_are_preserved():
    failure = {
        **tool_row(),
        "tool_result": "Error: upload refused",
        "content": "Error: upload refused",
    }
    other = {**bot_row("101"), "author": "Alice", "author_id": "42"}
    cleaned = canonical_history(
        [other, tool_row(), failure | {"message_id": "tool:10"}], "1", {"Maxwell"}
    )
    assert len(cleaned) == 3
    assert cleaned[-1]["is_tool"] and "Error" in cleaned[-1]["content"]


def test_missing_provenance_does_not_drop_legacy_echo_by_text_alone():
    missing = bot_row("101")
    missing.pop("timestamp")
    cleaned = canonical_history(
        [missing, bot_row("bot_send_message:9")], "1", {"Maxwell"}
    )
    assert len(cleaned) == 2


def test_two_identical_legacy_sends_use_each_visible_delivery_only_once():
    rows = [
        tool_row(),
        {**tool_row(), "message_id": "tool:10"},
        bot_row("bot_send_message:9"),
    ]
    cleaned = canonical_history(rows, "1", {"Maxwell"})
    assert len(cleaned) == 2
    assert [row["content"] for row in cleaned] == ["sent reply", "sent reply"]


def test_remote_send_receipt_is_not_replayed_as_a_reply_in_this_channel():
    row = {**tool_row(), "tool_params": {"channel_id": "other"}}
    cleaned = canonical_history([row], "1", {"Maxwell"}, channel_id="here")
    assert cleaned[0]["is_tool"]
    assert cleaned[0]["content"] == "send_message to other: __MESSAGE_SENT__"
