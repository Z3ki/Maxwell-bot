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
    if message.get("reasoning_content"):
        delta["reasoning_content"] = message["reasoning_content"]
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


class FakeReasoningOnlyResponse(FakeResponse):
    def _json_body(self):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "pong",
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


def test_vision_model_used_for_images():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "deepseek-v4-flash",
        10,
        0.5,
        api_key="pk",
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fk",
        vision_model="mimo-v2.5",
    )
    provider.available = True
    session = FakeSession()
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "look"}],
            media=[{"b64": "abc", "mime_type": "image/png"}],
            model="deepseek-v4-flash",
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.urls == ["http://primary.test/v1/chat/completions"]
    assert session.payloads[0]["model"] == "mimo-v2.5"
    content = session.payloads[0]["messages"][0]["content"]
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)


def test_vision_model_not_used_for_text():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "deepseek-v4-flash",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fk",
        vision_model="mimo-v2.5",
    )
    provider.available = True
    session = FakeSession()
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.payloads[0]["model"] == "deepseek-v4-flash"


def test_image_unsupported_skips_text_only_primary():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "deepseek-v4-flash",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fk",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                400,
                '{"error":{"message":"unknown variant `image_url`, expected `text`"}}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "look"}],
            media=[{"b64": "abc", "mime_type": "image/png"}],
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://fallback.test/v1/chat/completions",
    ]
    assert session.payloads[0]["model"] == "deepseek-v4-flash"
    assert session.payloads[1]["model"] == "fallback-model"


def test_reasoning_only_response_is_not_treated_as_empty():
    provider = OllamaProvider("http://example.test", "deepseek-v4-flash", 10, 0.5)
    provider.available = True
    session = FakeSession(FakeReasoningOnlyResponse())
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert message["content"] == "pong"

    asyncio.run(run())
    assert len(session.payloads) == 1


def test_403_region_error_skips_to_fallback_without_retry():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "deepseek-v4-flash",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fk",
    )
    provider.available = True
    provider._cooldown_seconds = 60
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                403,
                '{"type":"error","error":{"type":"RegionError","message":"China opt in"}}',
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
    assert session.urls == [
        "http://primary.test/v1/chat/completions",
        "http://fallback.test/v1/chat/completions",
    ]
    assert session.payloads[1]["model"] == "fallback-model"


def test_video_parts_are_not_attached():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "deepseek-v4-flash",
        10,
        0.5,
        vision_model="mimo-v2.5",
    )
    provider.available = True
    session = FakeSession()
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "look"}],
            media=[
                {"b64": "vid", "mime_type": "video/mp4"},
                {"b64": "img", "mime_type": "image/png"},
            ],
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    content = session.payloads[0]["messages"][0]["content"]
    types = [p.get("type") for p in content if isinstance(p, dict)]
    assert "image_url" in types
    assert "video_url" not in types
    assert session.payloads[0]["model"] == "mimo-v2.5"


# ---- deterministic 4xx handling: failover, media fallback, temperature ----


def test_openrouter_image_unsupported_routes_to_another_endpoint():
    """404 'No endpoints found that support image input' must not kill the turn."""
    provider = OllamaProvider(
        "http://primary.test/v1",
        "text-only-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                404,
                '{"error":{"message":"No endpoints found that support image input","code":404}}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "look"}],
            media=[{"b64": "img", "mime_type": "image/png"}],
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert len(session.payloads) == 2
    # Second attempt went to a different endpoint, still carrying the image.
    assert session.urls[0] != session.urls[1]
    parts = session.payloads[1]["messages"][0]["content"]
    assert any(p.get("type") == "image_url" for p in parts if isinstance(p, dict))


def test_media_unsupported_everywhere_falls_back_to_text_only():
    """When no endpoint accepts the image, answer the text instead of failing."""
    provider = OllamaProvider("http://primary.test/v1", "text-only-model", 10, 0.5)
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                404,
                '{"error":{"message":"No endpoints found that support image input"}}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "what is this"}],
            media=[{"b64": "img", "mime_type": "image/png"}],
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert len(session.payloads) == 2
    retried = session.payloads[1]["messages"][0]["content"]
    assert isinstance(retried, str)
    assert "what is this" in retried
    assert "attachment(s) omitted" in retried


def test_unhandled_4xx_fails_over_instead_of_raising():
    """404 model-unavailable on primary used to kill the turn outright."""
    provider = OllamaProvider(
        "http://primary.test/v1",
        "dead-slug",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                404,
                '{"error":{"message":"This model is unavailable for free.","code":404}}',
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
    assert len(session.payloads) == 2
    assert session.payloads[1]["model"] == "fallback-model"


def test_unhandled_4xx_single_endpoint_still_raises():
    """With nowhere to fail over to, the error must surface."""
    provider = OllamaProvider("http://primary.test/v1", "dead-slug", 10, 0.5)
    provider.available = True
    session = FakeSession(FakeErrorResponse(404, '{"error":{"message":"gone"}}'))
    provider._session = session

    async def run():
        with pytest.raises(RuntimeError, match="Provider API error: 404"):
            await provider.generate_chat_completion([{"role": "user", "content": "hi"}])

    asyncio.run(run())
    assert len(session.payloads) == 1


def test_temperature_constraint_is_learned_and_resent():
    """'only 0.6 is allowed' must resend at 0.6, not burn retries."""
    provider = OllamaProvider("http://primary.test/v1", "picky-model", 10, 0.9)
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                400,
                '{"error":{"message":"Upstream request failed: [invalid_request_error] '
                'invalid temperature: only 0.6 is allowed for this model"}}',
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
    assert session.payloads[0]["temperature"] == 0.9
    assert session.payloads[1]["temperature"] == 0.6
    # Learned, so the next call starts at the accepted value.
    assert provider._endpoint_temperatures["primary"] == 0.6


def test_media_incapable_endpoint_is_remembered_across_calls():
    """A text-only fallback shouldn't be re-offered images on every turn."""
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="text-only-fallback",
        vision_base_url="http://vision.test/v1",
        vision_model="vision-model",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                404,
                '{"error":{"message":"No endpoints found that support image input"}}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session
    # Pretend the vision endpoint is the one that answered second.
    provider._media_incapable.add("fallback")

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "look"}],
            media=[{"b64": "img", "mime_type": "image/png"}],
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    # Never routed the image to the known text-only fallback.
    assert all("fallback.test" not in url for url in session.urls)


def test_media_incapable_is_learned_from_a_404():
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="text-only-fallback",
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                404,
                '{"error":{"message":"No endpoints found that support image input"}}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        await provider.generate_chat_completion(
            [{"role": "user", "content": "look"}],
            media=[{"b64": "img", "mime_type": "image/png"}],
        )

    asyncio.run(run())
    assert "primary" in provider._media_incapable


def test_failover_extension_survives_the_last_attempt():
    """A deterministic 4xx on the FINAL attempt must still fail over.

    The retry loop bumps ``max_attempts`` when it decides to hand the call to
    another endpoint. That bump was written against a ``for attempt in
    range(1, max_attempts + 1)`` loop, whose bounds are snapshotted at entry —
    so the extension did nothing and the turn died with "Provider call failed
    after retries" while a healthy fallback sat idle.
    """
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        retry_attempts=1,  # the failure IS the last attempt
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                404,
                '{"error":{"message":"This model is unavailable for free."}}',
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
    assert len(session.urls) == 2
    assert "fallback.test" in session.urls[1]


def test_media_strip_retry_survives_the_last_attempt():
    """When every endpoint refuses the attachments on the final attempt,
    the text-only retry must actually run instead of dropping the turn."""
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        retry_attempts=1,
    )
    provider.available = True
    session = FakeSequenceSession(
        [
            FakeErrorResponse(
                400,
                '{"error":{"message":"unknown variant `image_url`, expected `text`"}}',
            ),
            FakeResponse(),
        ]
    )
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "what is this"}],
            media=[{"b64": "img", "mime_type": "image/png"}],
        )
        assert message["content"] == "ok"

    asyncio.run(run())
    assert len(session.payloads) == 2
    # Second attempt carries plain text, no image parts.
    retried = session.payloads[1]["messages"][-1]["content"]
    assert isinstance(retried, str)
    assert "attachment(s) omitted" in retried


def test_retry_loop_cannot_spin_forever_on_endless_deterministic_400s():
    """The while loop must still terminate when every reply is a fresh 400."""
    provider = OllamaProvider(
        "http://primary.test/v1",
        "primary-model",
        10,
        0.5,
        fallback_base_url="http://fallback.test/v1",
        fallback_model="fallback-model",
        retry_attempts=3,
    )
    provider.available = True

    class EndlessErrorSession(FakeSession):
        def post(self, url, json=None, timeout=None, headers=None):
            self.urls.append(url)
            self.payloads.append(copy.deepcopy(json))
            return FakeErrorResponse(400, '{"error":{"message":"nope"}}')

    session = EndlessErrorSession()
    provider._session = session

    async def run():
        with pytest.raises(Exception):
            await provider.generate_chat_completion(
                [{"role": "user", "content": "hi"}]
            )

    asyncio.run(run())
    assert len(session.urls) <= 3 + 2 * len(provider._endpoints) + 2
