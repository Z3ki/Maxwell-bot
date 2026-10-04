"""Exercise response normalization, retries, redaction, and cancellation together."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from maxwell_core.providers.errors import ProviderUnavailableError
from maxwell_core.providers.models import ProviderPolicy
from providers import OpenAICompatibleProvider


class Response:
    def __init__(self, payload=None, *, wire=None, status=200, error=""):
        self.payload = payload
        self.status = status
        self.error = error
        if wire is not None:
            self.content = SimpleNamespace(iter_any=lambda: self._chunks(wire))

    async def _chunks(self, wire):
        yield wire

    async def json(self):
        return self.payload

    async def text(self):
        return self.error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class Session:
    closed = False

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, *, json, headers, **kwargs):
        self.requests.append((url, copy.deepcopy(json), dict(headers)))
        return self.responses.pop(0)


def client(*responses, **kwargs):
    provider = OpenAICompatibleProvider(
        "https://primary.test/v1",
        "primary",
        4096,
        0.7,
        retry_attempts=1,
        empty_response_retries=0,
        **kwargs,
    )
    provider.available = True
    provider._session = Session(*responses)
    return provider


def completion(message):
    return {"choices": [{"message": message}]}


def wire(delta, *, finish="stop"):
    frame = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n".encode()


def test_completion_parts_are_normalized_consistently_without_mutating_source():
    raw = completion(
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "first"},
                " second",
                {"type": "image_url", "image_url": {"url": "x"}},
            ],
            "extra_content": {"google": {"thought_signature": "sig"}},
        }
    )
    before = copy.deepcopy(raw)
    provider = client(Response(raw))
    result = asyncio.run(provider.generate_chat_completion([], stream=False))
    assert result["content"] == "first second"
    assert result["extra_content"] == raw["choices"][0]["message"]["extra_content"]
    assert result.timing["content_chars"] == len(result["content"])
    assert raw == before


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": "secret-value"},
        {"choices": [None]},
        completion(None),
        completion([]),
        completion({"content": 7}),
        completion({"content": "ok", "tool_calls": {}}),
        completion({"content": "ok", "tool_calls": [None]}),
        completion({"content": "ok", "tool_calls": [{"function": "secret-value"}]}),
        completion({"content": "ok", "reasoning": {}}),
        completion({"content": "ok", "role": []}),
    ],
)
def test_malformed_completion_fails_safely_and_respects_attempt_budget(payload):
    provider = client(
        Response(payload),
        policy=ProviderPolicy(sensitive_credentials=True),
        api_key="secret-value",
    )
    with pytest.raises(ProviderUnavailableError) as caught:
        asyncio.run(provider.generate_chat_completion([], stream=False))
    assert "secret-value" not in str(caught.value)
    assert len(provider._session.requests) == 1


def test_response_reader_bounds_sensitive_stream_even_without_explicit_limit(
    monkeypatch,
):
    provider = client(
        Response(wire=wire({"content": "x" * 500})),
        policy=ProviderPolicy(sensitive_credentials=True),
    )
    monkeypatch.setattr(provider, "_response_limit", lambda: 128)
    with pytest.raises(RuntimeError, match="size limit"):
        asyncio.run(
            provider._read_completion_response(
                provider._session.responses[0],
                stream=True,
                byok_request=True,
                credential_secrets=(),
            )
        )


def test_byok_explicit_stream_request_scrubs_json_and_skips_progress_callbacks():
    credential = "private-token"
    message = {
        "content": credential,
        "tool_calls": [
            {
                "id": "call",
                "function": {"name": "web_search", "arguments": {"query": credential}},
                "extra_content": {"credential": credential},
            }
        ],
    }
    provider = client(
        Response(completion(message)),
        policy=ProviderPolicy(sensitive_credentials=True),
        api_key=credential,
    )
    tokens = []
    result = asyncio.run(
        provider.generate_chat_completion([], stream=True, on_token=tokens.append)
    )
    assert credential not in json.dumps(result)
    assert result["content"] == "[redacted]"
    assert result["tool_calls"][0]["extra_content"]["credential"] == "[redacted]"
    assert not tokens
    assert provider._session.requests[0][1]["stream"] is False


def test_failed_stream_falls_back_without_returning_partial_tools_or_crossing_keys(
    monkeypatch,
):
    partial = wire(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"name": "send_message", "arguments": '{"text":'},
                }
            ]
        },
        finish=None,
    ).replace(b"data: [DONE]\n\n", b"data: {invalid}\n\n")
    provider = client(
        Response(wire=partial),
        Response(wire=wire({"content": "fallback reply"})),
        api_key="primary-key",
        fallback_base_url="https://fallback.test/v1",
        fallback_model="fallback",
        fallback_api_key="fallback-key",
    )

    async def no_wait(_delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    result = asyncio.run(
        provider.generate_chat_completion([], retry_attempts=2, fast_fallback=True)
    )
    assert result["content"] == "fallback reply"
    assert result.get("tool_calls") is None
    requests = provider._session.requests
    assert [request[0] for request in requests] == [
        "https://primary.test/v1/chat/completions",
        "https://fallback.test/v1/chat/completions",
    ]
    assert [request[2]["Authorization"] for request in requests] == [
        "Bearer primary-key",
        "Bearer fallback-key",
    ]


def test_cancellation_during_stream_does_not_retry_or_return_partial_tools():
    async def run():
        reading = asyncio.Event()

        class WaitingResponse(Response):
            async def _chunks(self, _wire):
                yield wire(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "send_message",
                                    "arguments": '{"text":',
                                },
                            }
                        ]
                    },
                    finish=None,
                ).replace(b"data: [DONE]\n\n", b"")
                reading.set()
                await asyncio.Event().wait()

        provider = client(WaitingResponse(wire=b""))
        task = asyncio.create_task(
            provider.generate_chat_completion([], retry_attempts=3)
        )
        await reading.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(provider._session.requests) == 1
        assert not provider._timing_history

    asyncio.run(run())


@pytest.mark.parametrize("remaining", [10, 240, 1000])
def test_context_overflow_recovers_small_remaining_budget_on_final_attempt(remaining):
    context = 2048
    input_tokens = context - remaining
    requested = input_tokens + 4096
    provider = client(
        Response(
            status=400,
            error=f"maximum context length is {context} tokens. you requested about {requested} tokens",
        ),
        Response(completion({"content": "ok"})),
    )
    result = asyncio.run(provider.generate_chat_completion([], stream=False))
    assert result["content"] == "ok"
    budgets = [request[1]["max_tokens"] for request in provider._session.requests]
    assert budgets[0] == 4096
    assert 1 <= budgets[1] < remaining
    assert provider.max_tokens == 4096


def test_context_recovery_clamp_is_not_carried_to_larger_fallback():
    provider = client(
        Response(
            status=400,
            error="maximum context length is 2048 tokens. you requested about 5804 tokens",
        ),
        Response(status=404, error="model gone"),
        Response(completion({"content": "fallback"})),
        fallback_base_url="https://fallback.test/v1",
        fallback_model="large-model",
    )
    result = asyncio.run(provider.generate_chat_completion([], stream=False))
    assert result["content"] == "fallback"
    requests = provider._session.requests
    assert requests[1][0].startswith("https://primary.test")
    assert requests[1][1]["max_tokens"] < 340
    assert requests[2][0].startswith("https://fallback.test")
    assert requests[2][1]["max_tokens"] == 4096
