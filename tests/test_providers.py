import asyncio
import copy
import json

import pytest

from providers import (
    OllamaProvider,
    ProviderUsageExhaustedError,
    USAGE_EXHAUSTED_MESSAGE,
)


class _FakeAsyncStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


def _sse_chunks_for(body):
    if not isinstance(body, dict):
        return []
    choices = body.get("choices") or []
    if not choices:
        return []
    message = choices[0].get("message") or {}
    delta = {"role": message.get("role", "assistant")}
    if "content" in message:
        delta["content"] = message.get("content") or ""
    if message.get("tool_calls"):
        delta["tool_calls"] = [
            {"index": i, **tc} for i, tc in enumerate(message["tool_calls"])
        ]
    frame = {"choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]}
    return [
        f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n".encode("utf-8")
    ]


class FakeResponse:
    status = 200

    def __init__(self):
        self.content = _FakeAsyncStream(_sse_chunks_for(self._json_body()))

    def _json_body(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json_body()

    async def text(self):
        return ""


class FakeNoneJsonResponse(FakeResponse):
    """Returns None from json() — simulates a malformed/empty 200 response."""

    def _json_body(self):
        return None


class FakeToolCallResponse(FakeResponse):
    def _json_body(self):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "1"}],
                    }
                }
            ]
        }


class FakeErrorResponse(FakeResponse):
    def __init__(self, status, text):
        self.status = status
        self._text = text
        self.content = _FakeAsyncStream([])

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, response=None):
        self.payloads = []
        self.urls = []
        self.closed = False
        self.response = response or FakeResponse()

    def post(self, url, json=None, timeout=None, headers=None):
        self.urls.append(url)
        self.payloads.append(copy.deepcopy(json))
        return self.response


class FakeSequenceSession(FakeSession):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def post(self, url, json=None, timeout=None, headers=None):
        self.urls.append(url)
        self.payloads.append(copy.deepcopy(json))
        return self.responses.pop(0)


def test_generate_chat_completion_model_override():
    provider = OllamaProvider("http://example.test", "base-model", 10, 0.5)
    provider.available = True
    session = FakeSession()
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}],
            model="rem-model",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "ltm_list",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.payloads[0]["model"] == "rem-model"
    assert (
        session.payloads[0]["max_tokens"] == 10
    )  # configured max_tokens always included
    assert session.payloads[0]["tools"][0]["function"]["name"] == "ltm_list"


def test_generate_chat_completion_usage_exhausted_error():
    provider = OllamaProvider("http://example.test", "base-model", 10, 0.5)
    provider.available = True
    session = FakeSession(
        FakeErrorResponse(
            429,
            '{"error":{"code":"model_cooldown","message":"All credentials are cooling down"}}',
        )
    )
    provider._session = session

    async def run():
        with pytest.raises(ProviderUsageExhaustedError) as exc_info:
            await provider.generate_chat_completion([{"role": "user", "content": "hi"}])
        assert exc_info.value.user_message == USAGE_EXHAUSTED_MESSAGE

    asyncio.run(run())
    assert len(session.payloads) == 1


def test_generate_chat_completion_falls_back_to_secondary_provider():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fallback-key",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(503, "down"),
            FakeErrorResponse(503, "down"),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://primary.test/v1/chat/completions",
        "http://fallback.test/v1/chat/completions",
    ]
    assert session.payloads[0]["model"] == "primary-model"
    assert session.payloads[1]["model"] == "primary-model"
    assert session.payloads[2]["model"] == "fallback-model"
    assert (
        session.payloads[0]["max_tokens"] == 10
    )  # configured max_tokens always included
    assert session.payloads[2]["max_tokens"] == 10
    assert session.payloads[2]["reasoning"] == {"effort": "none"}


def test_generate_chat_completion_retries_primary_before_fallback():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fallback-key",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(503, "down"),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://primary.test/v1/chat/completions",
    ]
    assert session.payloads[0]["model"] == "primary-model"
    assert session.payloads[1]["model"] == "primary-model"


def test_429_rate_limit_skips_to_fallback_without_doomed_retry():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fallback-key",
    )
    provider.available = True
    # No backoff sleep on the single fallback step.
    provider._cooldown_seconds = 60
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                429,
                '{"error":{"code":429,"message":"xiaomi/mimo-v2.5 is temporarily rate-limited upstream. Please retry shortly, or add your own key to accumulate your rate limits"}}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    # Only ONE primary call (the 429) then immediate fallback — no second doomed
    # primary retry, no 2s wait.
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://fallback.test/v1/chat/completions",
    ]
    assert session.payloads[0]["model"] == "primary-model"
    assert session.payloads[1]["model"] == "fallback-model"
    # Primary is now cooling: a follow-up call must skip straight to fallback.
    session2 = FakeSequenceSession([FakeResponse()])
    provider._session = session2

    async def run2():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run2())
    assert session2.urls == ["http://fallback.test/v1/chat/completions"]


def test_generate_response_returns_native_tool_calls():
    """generate_response now supports native tool_calls instead of rejecting them."""
    provider = OllamaProvider("http://example.test", "base-model", 10, 0.5)
    provider.available = True
    provider._session = FakeSession(FakeToolCallResponse())

    async def run():
        content = await provider.generate_response([{"role": "user", "content": "hi"}])
        # Content may be empty when the model only emits tool_calls
        assert content == ""
        assert len(provider._last_tool_calls) == 1
        assert provider._last_tool_calls[0]["id"] == "1"

    asyncio.run(run())


def test_context_overflow_clamp_survives_retry():
    provider = OllamaProvider(
        "http://example.test", "base-model", 12000, 0.5, retry_attempts=2
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                400,
                "maximum context length is 10000 tokens. you requested about 13000 tokens",
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def no_wait_retry(*args, **kwargs):
        return True

    provider._retry_after_attempt = no_wait_retry

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.payloads[0]["max_tokens"] == 12000
    assert session.payloads[1]["max_tokens"] == 8488


def test_none_json_body_retries_and_falls_back():
    """A 200 response with a None/missing JSON body should not crash with
    AttributeError — it should retry/fallback like any other failed response."""
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fallback-key",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeNoneJsonResponse(),  # primary attempt 1 — None body
            FakeNoneJsonResponse(),  # primary attempt 2 — None body
            FakeResponse(),  # fallback attempt 3 — success
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://primary.test/v1/chat/completions",
        "http://fallback.test/v1/chat/completions",
    ]


def test_degraded_endpoint_skips_to_fallback_without_retry():
    """A 400 'DEGRADED function cannot be invoked' should cool the endpoint and
    fall back immediately — no wasted retries on the same degraded endpoint."""
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fallback-key",
    )
    provider.available = True
    provider._cooldown_seconds = 60
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                400,
                '{"status":400,"title":"Bad Request","detail":"Function id \'abc\': DEGRADED function cannot be invoked"}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    # Only ONE primary call (the DEGRADED 400) then immediate fallback — no
    # second doomed primary retry, no 2s wait.
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://fallback.test/v1/chat/completions",
    ]
    assert session.payloads[0]["model"] == "primary-model"
    assert session.payloads[1]["model"] == "fallback-model"
    # Primary is now cooling: a follow-up call must skip straight to fallback.
    session2 = FakeSequenceSession([FakeResponse()])
    provider._session = session2

    async def run2():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run2())
    assert session2.urls == ["http://fallback.test/v1/chat/completions"]
