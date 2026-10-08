"""Offline regressions for integration boundary and persistence failures."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from providers import OpenAICompatibleProvider
from utils import JsonStateStore, render_discord_context_text


@pytest.mark.parametrize("operation", ["patch", "log"])
@pytest.mark.parametrize("raw", ["{broken", "[]", ""])
def test_state_mutations_preserve_corrupt_files(tmp_path, operation, raw):
    store = JsonStateStore(str(tmp_path), state_file="state.json", log_file="log.json")
    path = store.state_file if operation == "patch" else store.log_file
    path.write_text(raw)
    with pytest.raises(ValueError):
        asyncio.run(
            store.patch_state({"new": 1})
            if operation == "patch"
            else store.append_log_entry({"new": 1})
        )
    assert path.read_text() == raw


def test_state_mutations_initialize_missing_files(tmp_path):
    store = JsonStateStore(str(tmp_path), state_file="state.json", log_file="log.json")
    asyncio.run(store.patch_state({"new": 1}))
    asyncio.run(store.append_log_entry({"new": 1}))
    assert json.loads(store.state_file.read_text()) == {"new": 1}
    assert json.loads(store.log_file.read_text()) == {"entries": [{"new": 1}]}


def test_media_annotation_uses_path_not_query_extension():
    url = "https://example.test/photo.png?name=download.jpg#preview"
    message = SimpleNamespace(content=url)
    assert f"[media URL: image {url}]" in render_discord_context_text(message)


def test_provider_tool_fallback_preserves_stream_callbacks(monkeypatch):
    provider = OpenAICompatibleProvider("http://example.test", "test", 10, 0.5)
    complete = AsyncMock(
        side_effect=[RuntimeError("tools not supported"), {"content": "ok"}]
    )
    monkeypatch.setattr(provider, "generate_chat_completion", complete)
    token_cb, tool_cb = object(), object()
    asyncio.run(
        provider.generate_response(
            [],
            tools=[{}],
            on_token=token_cb,
            on_tool_call_name=tool_cb,
            custom_tool_calls=True,
        )
    )
    kwargs = complete.await_args_list[1].kwargs
    assert kwargs.get("on_token") is token_cb
    assert kwargs.get("on_tool_call_name") is tool_cb
    assert kwargs.get("custom_tool_calls") is True


class _Stream:
    def __init__(self, content="ok", usage=None):
        self.body = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
            ]
        }
        if usage:
            self.body["usage"] = usage

    async def iter_any(self):
        yield ("data: " + json.dumps(self.body) + "\n\ndata: [DONE]\n\n").encode()


class _Response:
    status = 200

    def __init__(self, content="ok", usage=None, on_exit=None):
        self.content = _Stream(content, usage)
        self.on_exit = on_exit

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.on_exit:
            await self.on_exit()


class _Session:
    closed = False

    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []

    def post(self, url, *, json, **kwargs):
        self.payloads.append(json)
        return next(self.responses)


def test_provider_preserves_existing_multimodal_parts():
    provider = OpenAICompatibleProvider("http://example.test", "test", 10, 0.5)
    provider.available = True
    provider._session = session = _Session([_Response()])
    original_parts = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "https://example.test/old.png"}},
    ]
    asyncio.run(
        provider.generate_response(
            [{"role": "user", "content": original_parts}],
            media=[{"b64": "abc", "mime_type": "image/png"}],
        )
    )
    parts = session.payloads[0]["messages"][0]["content"]
    assert parts[:2] == original_parts
    assert len(parts) == 3


def test_provider_embedded_media_routes_to_vision():
    provider = OpenAICompatibleProvider(
        "http://example.test", "text", 10, 0.5, vision_model="vision"
    )
    provider.available = True
    provider._session = session = _Session([_Response()])
    asyncio.run(
        provider.generate_response(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/image.png"},
                        },
                    ],
                }
            ]
        )
    )
    assert session.payloads[0]["model"] == "vision"


def test_provider_usage_is_not_swapped_during_response_cleanup():
    async def run():
        first_cleaning, second_done = asyncio.Event(), asyncio.Event()

        async def first_exit():
            first_cleaning.set()
            await second_done.wait()

        provider = OpenAICompatibleProvider("http://example.test", "test", 10, 0.5)
        provider.available = True
        provider._session = _Session(
            [
                _Response(
                    "first", {"prompt_tokens": 11, "completion_tokens": 1}, first_exit
                ),
                _Response("second", {"prompt_tokens": 22, "completion_tokens": 2}),
            ]
        )
        first = asyncio.create_task(provider.generate_response([]))
        await first_cleaning.wait()
        second = await provider.generate_response([])
        second_done.set()
        first = await first
        assert first.usage["prompt_tokens"] == 11
        assert second.usage["prompt_tokens"] == 22

    asyncio.run(run())


def test_repetition_guards_preserve_unclosed_code_fences():
    from response_guard import break_echo_loop, scrub_repetitions

    text = "```python\n" + 'print("same same same")\n' * 20
    assert scrub_repetitions(text) == text
    assert break_echo_loop(text) == text


def test_state_updates_from_separate_store_instances_do_not_lose_keys(tmp_path):
    async def run():
        stores = [
            JsonStateStore(str(tmp_path), state_file="state.json", log_file="log.json")
            for _ in range(12)
        ]
        await asyncio.gather(
            *(store.patch_state({str(i): i}) for i, store in enumerate(stores))
        )
        assert await stores[0].load_state() == {str(i): i for i in range(12)}

    asyncio.run(run())


@pytest.mark.parametrize(
    "error",
    ["unknown field stream_options", "invalid temperature: only 0.6 is allowed"],
)
def test_provider_parameter_repair_retries_same_endpoint(error):
    class ErrorResponse(_Response):
        status = 400

        async def text(self):
            return error

    provider = OpenAICompatibleProvider(
        "http://primary.test",
        "primary",
        10,
        0.5,
        fallback_base_url="http://fallback.test",
        fallback_model="fallback",
    )
    provider.available = True
    provider._session = session = _Session([ErrorResponse(), _Response()])
    asyncio.run(provider.generate_response([], fast_fallback=True))
    assert [p["model"] for p in session.payloads] == ["primary", "primary"]



