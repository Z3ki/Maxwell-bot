"""Binary media stays in provider parts, never in persisted chat text."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot
from media_payloads import (
    merge_followup_media,
    sanitize_media_memory,
    strip_media_payloads,
)
from rag_memory import MemoryRequester, RAGMemoryManager, _strip_for_embedding


@pytest.mark.parametrize("kind", ["IMAGE", "AUDIO"])
@pytest.mark.parametrize("closed", [True, False])
def test_payloads_are_removed_before_clipping(kind, closed):
    payload = "QUFB" * 10_000
    suffix = f"__END_{kind}_B64__\nUseful result." if closed else ""
    raw = f"Loaded media.\n__{kind}_B64__{payload}{suffix}"
    clean = strip_media_payloads(raw)
    assert "Loaded media." in clean
    assert payload[:100] not in clean
    assert "_B64__" not in clean
    if closed:
        assert "Useful result." in clean
    assert payload[:100] not in _strip_for_embedding(raw)


def test_metadata_scrubbing_preserves_the_input_and_normal_text():
    metadata = {"media": [{"b64": "QUFB", "mime_type": "image/jpeg"}]}
    cleaned = sanitize_media_memory(metadata)
    assert cleaned["media"][0]["b64"] == "[binary payload omitted]"
    assert metadata["media"][0]["b64"] == "QUFB"
    assert strip_media_payloads("Normal text, code and https://e.com/a.png") == (
        "Normal text, code and https://e.com/a.png"
    )


@pytest.mark.parametrize("kind,tool", [("IMAGE", "see_image"), ("AUDIO", "see_media")])
def test_dispatch_extracts_media_but_never_persists_it(monkeypatch, kind, tool):
    async def run():
        memory = SimpleNamespace(add_to_channel_memory=AsyncMock())
        owner = SimpleNamespace(_control={}, tools={}, memory=memory)
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)
        payload = "QUFB" * 2000
        raw = f"Tool {tool}: Loaded media.\n__{kind}_B64__{payload}__END_{kind}_B64__"
        monkeypatch.setattr(
            MaxwellBot, "_execute_tool_by_name", AsyncMock(return_value=raw)
        )
        calls = [
            {
                "id": "inspect",
                "type": "function",
                "function": {
                    "name": tool,
                    "arguments": '{"url":"https://e.com/media"}',
                },
            }
        ]
        _, results, images = await MaxwellBot._process_native_tool_calls(
            owner, message, "", calls, include_images=True
        )
        memory.add_to_channel_memory.assert_awaited_once()
        saved = memory.add_to_channel_memory.call_args.args[1]
        assert payload[:100] not in json.dumps(saved)
        assert "Loaded media." in saved["tool_result"]
        assert payload[:100] not in json.dumps(owner._last_native_followup_messages)
        assert payload[:100] not in results[0]
        if kind == "IMAGE":
            assert images == [payload]
        else:
            assert owner._last_native_tool_media[0]["b64"] == payload

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["IMAGE", "AUDIO"])
def test_database_stores_clean_results_and_filters_legacy_rows(
    tmp_path, monkeypatch, kind
):
    async def run():
        manager = RAGMemoryManager(str(tmp_path))
        monkeypatch.setattr(manager, "_embed_and_store", AsyncMock())
        raw = f"Loaded media.\n__{kind}_B64__" + "QUFB" * 3000
        # No closing marker: simulate the old 8000-character database cap.
        requester = MemoryRequester(user_id="u", channel_id="c", is_dm=True)
        message = {
            "message_id": "tool1",
            "author": "Tool",
            "content": raw,
            "is_tool": True,
            "tool_name": "see_media",
            "tool_result": raw,
        }
        await manager.add_to_channel_memory("c", message)
        row = manager._db.execute(
            "SELECT content, metadata FROM vectors WHERE id='tool1'"
        ).fetchone()
        assert "QUFB" not in row["content"] + row["metadata"]
        # Bypass the fixed writer to reproduce an already-contaminated row.
        manager._db.execute(
            "UPDATE vectors SET content=?, metadata=? WHERE id='tool1'",
            (raw[:8000], json.dumps({"is_tool": True, "tool_result": raw})),
        )
        history = await manager.get_channel_memory("c", requester=requester)
        assert len(history) == 1
        assert "QUFB" not in json.dumps(history)
        assert "Loaded media." in history[0]["content"]
        assert message["content"] == raw
        await asyncio.gather(*manager._embed_tasks)
        manager._db.close()

    asyncio.run(run())


def test_followups_retain_original_image_audio_and_new_tool_media():
    image = {
        "b64": "original-image",
        "mime_type": "image/jpeg",
        "filename": "photo.jpg",
    }
    audio = {"b64": "original-audio", "mime_type": "audio/ogg"}
    tool_audio = {"b64": "tool-audio", "mime_type": "audio/wav"}
    original = [image, audio]
    first = merge_followup_media(
        original, [tool_audio], ["original-image", "tool-image"]
    )
    assert first == [
        image,
        audio,
        tool_audio,
        {"b64": "tool-image", "mime_type": "image/png"},
    ]
    second = merge_followup_media(
        original, [tool_audio], ["tool-image", "second-image"]
    )
    assert second[:2] == original
    assert len(second) == 5
    first[0]["filename"] = "changed"
    assert image["filename"] == "photo.jpg"
    assert merge_followup_media(original, [], []) == original
    assert merge_followup_media([], [], []) == []


def test_tool_image_keeps_its_real_jpeg_mime_type(monkeypatch):
    async def run():
        owner = SimpleNamespace(_control={}, tools={})
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)
        raw = "Tool see_image: Loaded.\n__IMAGE_B64__data:image/jpeg;base64,/9j/AA==__END_IMAGE_B64__"
        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", AsyncMock(return_value=raw))
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        calls = [{"id": "image", "type": "function", "function": {"name": "see_image", "arguments": "{}"}}]
        _, results, legacy_images = await MaxwellBot._process_native_tool_calls(
            owner, message, "", calls, include_images=True
        )
        assert legacy_images == []
        assert owner._last_native_tool_media[0]["mime_type"] == "image/jpeg"
        assert owner._last_native_tool_media[0]["b64"] == "/9j/AA=="
        assert "/9j/AA==" not in str(results)

    asyncio.run(run())
