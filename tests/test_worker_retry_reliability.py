"""Worker provider calls survive dropped connections and log why they fail."""

import asyncio
import json
import logging

import aiohttp
import pytest

from maxwell_core.providers.base import ChatProvider
from maxwell_core.providers.errors import ProviderUnavailableError
from maxwell_core.providers.factory import openai_compat_provider
from maxwell_core.providers.models import ProviderResult
from maxwell_core.providers.routing import ProviderRouter
from plugins.maxwell_extras.task_workers import WORKER_RETRY_ATTEMPTS
from plugins.maxwell_extras.worker_providers import select_worker_provider
from providers import OpenAICompatibleProvider
from test_premium_task_workers import TASKS, setup
from test_providers import FakeResponse, FakeSequenceSession, _FakeAsyncStream


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds, *args, **kwargs):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


class _DisconnectOnPost(FakeResponse):
    async def __aenter__(self):
        raise aiohttp.ServerDisconnectedError()


class _BrokenStream(_FakeAsyncStream):
    def __init__(self, error):
        super().__init__([])
        self.error = error

    async def iter_any(self):
        frame = {"choices": [{"index": 0, "delta": {"content": "par"}}]}
        yield f"data: {json.dumps(frame)}\n\n".encode()
        raise self.error


class _PayloadDropped(FakeResponse):
    def __init__(self):
        super().__init__()
        self.content = _BrokenStream(
            aiohttp.ClientPayloadError("Response payload is not completed")
        )


class _ReadTimeout(FakeResponse):
    def __init__(self):
        super().__init__()
        self.content = _BrokenStream(asyncio.TimeoutError())


class _TruncatedStream(FakeResponse):
    """Connection closed cleanly mid-answer: no finish_reason and no [DONE]."""

    def __init__(self):
        super().__init__()
        frame = {"choices": [{"index": 0, "delta": {"content": "half"}}]}
        self.content = _FakeAsyncStream([f"data: {json.dumps(frame)}\n\n".encode()])


class _TruncatedToolStream(FakeResponse):
    """A cut-off stream whose tool call never got valid arguments."""

    def __init__(self):
        super().__init__()
        frame = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "shell", "arguments": 7}}
                        ]
                    },
                }
            ]
        }
        self.content = _FakeAsyncStream([f"data: {json.dumps(frame)}\n\n".encode()])


def _client(responses, **kwargs):
    provider = OpenAICompatibleProvider(
        "http://primary.test/v1", "model", 10, 0.5, retry_attempts=1, **kwargs
    )
    provider.available = True
    provider._session = FakeSequenceSession(responses)
    return provider


@pytest.mark.parametrize(
    "dropped",
    [_DisconnectOnPost, _PayloadDropped, _ReadTimeout, _TruncatedStream],
)
def test_dropped_connection_is_retried_with_backoff_within_worker_budget(
    dropped, no_sleep
):
    provider = _client([dropped(), FakeResponse()])
    message = asyncio.run(
        provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}],
            retry_attempts=WORKER_RETRY_ATTEMPTS,
        )
    )
    assert message["content"] == "ok"
    assert len(provider._session.urls) == 2
    assert no_sleep and no_sleep[0] >= 1.5  # backoff, not an immediate re-hit


def test_dropped_connections_exhaust_three_attempts_then_fail_retryable(no_sleep):
    provider = _client([_PayloadDropped(), _DisconnectOnPost(), _TruncatedStream()])
    with pytest.raises(ProviderUnavailableError) as raised:
        asyncio.run(
            provider.generate_chat_completion(
                [{"role": "user", "content": "hi"}],
                retry_attempts=WORKER_RETRY_ATTEMPTS,
            )
        )
    assert raised.value.retryable
    assert len(provider._session.urls) == 3


def test_retry_log_reports_the_per_request_budget(no_sleep, caplog):
    provider = _client([_PayloadDropped(), FakeResponse()])
    with caplog.at_level(logging.WARNING, logger="providers"):
        asyncio.run(
            provider.generate_chat_completion(
                [{"role": "user", "content": "hi"}], retry_attempts=3
            )
        )
    assert "(attempt 1/3)" in caplog.text


def test_truncated_tool_stream_is_not_mistaken_for_tool_rejection(no_sleep):
    provider = _client([_TruncatedToolStream()])
    tools = [{"type": "function", "function": {"name": "shell", "parameters": {}}}]
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(
            provider.generate_response([{"role": "user", "content": "hi"}], tools=tools)
        )
    # One attempt, and no second request with the tools stripped.
    assert len(provider._session.payloads) == 1


class _FlakyEndpoint(ChatProvider):
    def __init__(self, name, failures):
        self.name = name
        self.failures = list(failures)
        self.calls = []

    async def initialize(self):
        return True

    async def generate_response(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return ProviderResult("done", provider=self.name)


def test_router_honors_worker_budget_and_retries_raw_transport_errors(no_sleep):
    primary = _FlakyEndpoint(
        "primary",
        [
            aiohttp.ServerDisconnectedError(),
            aiohttp.ClientPayloadError("Response payload is not completed"),
        ],
    )
    vision = _FlakyEndpoint("vision", [])
    router = ProviderRouter(primary, None, vision, retry_attempts=1)
    result = asyncio.run(
        router.generate_response(
            [{"role": "user", "content": "hi"}],
            retry_attempts=WORKER_RETRY_ATTEMPTS,
        )
    )
    assert result == "done" and len(primary.calls) == 3
    # The router owns the budget; each client call stays single-shot.
    assert all(call["retry_attempts"] == 1 for call in primary.calls)
    assert no_sleep == [1.5, 3.0]


def test_router_transport_error_text_is_not_surfaced(no_sleep):
    primary = _FlakyEndpoint(
        "primary", [aiohttp.ClientConnectionError("https://h/v1?key=sk-secret")]
    )
    router = ProviderRouter(primary, None, _FlakyEndpoint("vision", []))
    with pytest.raises(ProviderUnavailableError) as raised:
        asyncio.run(
            router.generate_response(
                [{"role": "user", "content": "hi"}], retry_attempts=1
            )
        )
    assert "sk-secret" not in str(raised.value)
    assert "ClientConnectionError" in str(raised.value)


def test_worker_requests_three_provider_attempts(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    asyncio.run(worker.run(msg, tasks=TASKS))
    assert WORKER_RETRY_ATTEMPTS == 3
    assert bot._generate_response.await_count == 2
    assert all(
        call.kwargs["retry_attempts"] == 3
        for call in bot._generate_response.await_args_list
    )


def test_operator_worker_client_defaults_to_three_attempts(tmp_path):
    bot, _, _, _ = setup(tmp_path)
    bot._make_chat_provider = openai_compat_provider
    bot.config.AI_WORKER_MODEL = "worker-model"
    bot.config.AI_WORKER_BASE_URL = "https://worker.example/v1"
    bot.config.AI_WORKER_API_KEY = "sk-worker-key-123456"

    async def run():
        provider, owned = await select_worker_provider(bot, "123")
        try:
            assert owned and provider.retry_attempts == 3
        finally:
            await provider.close()

    asyncio.run(run())


def test_worker_failure_is_logged_with_traceback_and_redacted(tmp_path, caplog):
    bot, msg, shell, worker = setup(tmp_path)
    bot.ai_provider.api_key = "byok-private-key-987"
    bot._generate_response.side_effect = ProviderUnavailableError(
        "Provider call failed: Server disconnected "
        "https://user:pw@proxy.example/v1/chat?token=abc123 byok-private-key-987"
    )
    with caplog.at_level(logging.ERROR, logger="plugins.maxwell_extras.task_workers"):
        report = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert report["status"] == "failed"
    assert "ProviderUnavailableError: Provider call failed" in report["report"]
    logged = caplog.text
    assert "Task worker job failed: ProviderUnavailableError" in logged
    assert "Traceback (most recent call last)" in logged
    assert "proxy.example" in logged
    for leaked in ("abc123", "token=", "user:pw", "byok-private-key-987"):
        assert leaked not in logged and leaked not in report["report"]


def test_non_provider_failure_report_keeps_only_the_type(tmp_path, caplog):
    bot, msg, shell, worker = setup(tmp_path)
    bot._generate_response.side_effect = KeyError("private task detail")
    with caplog.at_level(logging.ERROR, logger="plugins.maxwell_extras.task_workers"):
        report = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert "(KeyError)" in report["report"]
    assert "private task detail" not in report["report"]
    assert "KeyError" in caplog.text and "Traceback" in caplog.text


def test_tool_failure_is_logged(tmp_path, caplog):
    bot, msg, shell, worker = setup(tmp_path)

    async def broken_executor(*args, **kwargs):
        raise OSError("disk full")

    bot._execute_tool_by_name = broken_executor
    with caplog.at_level(logging.ERROR, logger="plugins.maxwell_extras.task_workers"):
        report = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert report["completed"] == [{"tool": "shell", "status": "error"}]
    assert "Task worker tool shell failed: OSError: disk full" in caplog.text
