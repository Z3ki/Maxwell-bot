import asyncio
import copy

import pytest

from providers import OllamaProvider, ProviderUsageExhaustedError, USAGE_EXHAUSTED_MESSAGE


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def text(self):
        return ""


class FakeToolCallResponse(FakeResponse):
    async def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}}]}


class FakeErrorResponse(FakeResponse):
    def __init__(self, status, text):
        self.status = status
        self._text = text

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
            tools=[{"type": "function", "function": {"name": "ltm_list", "parameters": {"type": "object", "properties": {}}}}],
        )
        assert message["content"] == "ok"
    asyncio.run(run())
    assert session.payloads[0]["model"] == "rem-model"
    assert session.payloads[0]["max_tokens"] == 10  # configured max_tokens always included
    assert session.payloads[0]["tools"][0]["function"]["name"] == "ltm_list"


def test_generate_chat_completion_usage_exhausted_error():
    provider = OllamaProvider("http://example.test", "base-model", 10, 0.5)
    provider.available = True
    session = FakeSession(FakeErrorResponse(429, '{"error":{"code":"model_cooldown","message":"All credentials are cooling down"}}'))
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
    session = FakeSequenceSession([
        FakeErrorResponse(503, "down"),
        FakeErrorResponse(503, "down"),
        FakeResponse(),
    ])
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion([{"role": "user", "content": "hi"}])
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
    assert session.payloads[0]["max_tokens"] == 10  # configured max_tokens always included
    assert session.payloads[2]["max_tokens"] == 10
    assert session.payloads[2]["reasoning"] == {"exclude": True}


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
    session = FakeSequenceSession([
        FakeErrorResponse(503, "down"),
        FakeResponse(),
    ])
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion([{"role": "user", "content": "hi"}])
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://primary.test/v1/chat/completions",
    ]
    assert session.payloads[0]["model"] == "primary-model"
    assert session.payloads[1]["model"] == "primary-model"


def test_generate_response_rejects_native_tool_calls():
    provider = OllamaProvider("http://example.test", "base-model", 10, 0.5)
    provider.available = True
    provider._session = FakeSession(FakeToolCallResponse())

    async def run():
        with pytest.raises(RuntimeError, match="Native provider tool_calls"):
            await provider.generate_response([{"role": "user", "content": "hi"}])

    asyncio.run(run())


def test_context_overflow_clamp_survives_retry():
    provider = OllamaProvider("http://example.test", "base-model", 12000, 0.5, retry_attempts=2)
    provider.available = True
    session = FakeSequenceSession([
        FakeErrorResponse(400, "maximum context length is 10000 tokens. you requested about 13000 tokens"),
        FakeResponse(),
    ])
    provider._session = session

    async def no_wait_retry(*args, **kwargs):
        return True

    provider._retry_after_attempt = no_wait_retry

    async def run():
        message = await provider.generate_chat_completion([{"role": "user", "content": "hi"}])
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.payloads[0]["max_tokens"] == 12000
    assert session.payloads[1]["max_tokens"] == 8488
