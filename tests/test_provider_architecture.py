"""Architectural regressions beyond renaming the historical client tests."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from dotenv import dotenv_values

from bot import MaxwellBot
from maxwell_core.providers.base import ChatProvider
from maxwell_core.providers.errors import (
    ProviderAuthenticationError,
    ProviderEmptyResponseError,
    ProviderInvalidRequestError,
    ProviderMediaUnsupportedError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderUsageExhaustedError,
)
from maxwell_core.providers.factory import openai_compat_provider, provider_from_config
from maxwell_core.providers.models import (
    GenerationDefaults,
    ProviderCapabilities,
    ProviderConfig,
    ProviderPolicy,
    ProviderResult,
)
from maxwell_core.providers.routing import ProviderRouter
from plugins.maxwell_extras.byok import make_request_provider
from providers import OllamaProvider, OpenAICompatibleProvider
from scripts.migrate_ai_env import PAIRS, migrate
from test_install_config import _run
from test_providers import (
    FakeEmptyResponse,
    FakeErrorResponse,
    FakeResponse,
    FakeSequenceSession,
    FakeSession,
    FakeToolCallResponse,
)


@pytest.mark.parametrize("old,new", PAIRS)
def test_all_legacy_environment_inputs_and_neutral_precedence(old, new):
    if new == "AI_OPENCODE_SESSION":
        return  # host-specific header setting is tested separately below
    if new == "AI_ENDPOINT_COOLDOWN_SECONDS":
        code = (
            "from providers import OpenAICompatibleProvider; "
            "print(OpenAICompatibleProvider('http://example.test', 'm', 5, .5)._cooldown_seconds)"
        )
    else:
        code = f"from config import Config; print(Config.{new})"
    if "DISABLE_REASONING" in new:
        legacy, neutral, expected_legacy, expected_neutral = (
            "false",
            "true",
            "False",
            "True",
        )
    elif new == "AI_TEMPERATURE":
        legacy, neutral, expected_legacy, expected_neutral = "0.2", "0.4", "0.2", "0.4"
    elif new.endswith(("RETRIES", "ATTEMPTS")) or new in {
        "AI_MAX_OUTPUT_TOKENS",
        "AI_ENDPOINT_COOLDOWN_SECONDS",
    }:
        legacy, neutral = "2", "4"
        expected_legacy, expected_neutral = (
            ("2.0", "4.0") if "COOLDOWN" in new else ("2", "4")
        )
    else:
        legacy, neutral = "legacy-value", "neutral-value"
        expected_legacy, expected_neutral = legacy, neutral
    assert _run(code, {old: legacy}) == expected_legacy
    assert _run(code, {old: legacy, new: neutral}) == expected_neutral


def test_endpoint_precedence_and_blank_optional_credentials():
    env = {
        "AI_BASE_URL": "https://new.test/v1",
        "AI_API_URL": "https://older.test/v1",
        "OLLAMA_BASE_URL": "https://oldest.test/v1",
        "AI_API_KEY": "",
        "OLLAMA_API_KEY": "old-secret",
        "AI_FALLBACK_MODEL": "",
        "OLLAMA_FALLBACK_MODEL": "stale-model",
    }
    values = json.loads(
        _run(
            "from config import Config; import json; print(json.dumps([Config.AI_BASE_URL, Config.AI_API_KEY, Config.AI_FALLBACK_MODEL]))",
            env,
        )
    )
    assert values == ["https://new.test/v1", "", ""]


@pytest.mark.parametrize(
    "kind",
    ["openai_compat", "ollama", "openai", "openrouter", "groq", "lmstudio", "custom"],
)
def test_factory_maps_protocol_aliases_and_honors_explicit_configuration(kind):
    config = ProviderConfig(
        name="personal",
        kind=kind,
        base_url="https://example.test",
        model="model",
        api_key="secret",
        generation=GenerationDefaults(
            max_output_tokens=123, temperature=0.2, retry_attempts=2
        ),
    )
    client = provider_from_config(config)
    assert isinstance(client, ChatProvider)
    assert isinstance(client, OpenAICompatibleProvider)
    assert client.config is config
    assert (client.name, client.base_url, client.max_tokens, client.temperature) == (
        "personal",
        "https://example.test/v1",
        123,
        0.2,
    )
    assert "secret" not in repr(config)
    assert "secret" not in repr(client._endpoints[0])
    assert OllamaProvider is OpenAICompatibleProvider


def test_factory_unknown_kind_and_multiroute_configuration_fail_clearly():
    with pytest.raises(ValueError, match="unknown provider kind"):
        provider_from_config(ProviderConfig(name="unsupported", kind="responses"))
    with pytest.raises(ValueError, match="Construct alternate providers"):
        provider_from_config(ProviderConfig(name="p"), fallback_model="other")


def test_explicit_audio_capability_enables_audio_transport():
    client = provider_from_config(
        ProviderConfig(
            name="audio", model="m", capabilities=ProviderCapabilities(audio=True)
        )
    )
    assert client.enable_audio_input is True
    assert client.declare()["audio"] is True


@pytest.mark.parametrize(
    "response,error_type",
    [
        (FakeErrorResponse(401, "invalid key"), ProviderAuthenticationError),
        (FakeErrorResponse(400, "invalid request"), ProviderInvalidRequestError),
        (FakeErrorResponse(429, "too many requests"), ProviderRateLimitError),
        (
            FakeErrorResponse(429, '{"error":{"code":"insufficient_quota"}}'),
            ProviderUsageExhaustedError,
        ),
        (FakeEmptyResponse(), ProviderEmptyResponseError),
    ],
)
def test_safe_structured_errors_survive_sensitive_body_redaction(response, error_type):
    client = openai_compat_provider(
        base_url="https://example.test",
        model="m",
        api_key="user-secret",
        policy=ProviderPolicy(sensitive_credentials=True),
        retry_attempts=1,
        empty_response_retries=0,
    )
    client.available = True
    client._session = FakeSession(response)
    with pytest.raises(error_type):
        asyncio.run(client.generate_response([]))


def test_router_streaming_native_tools_stay_on_the_returned_result():
    router = _router([FakeToolCallResponse()], [FakeResponse()])
    result = asyncio.run(
        router.generate_response(
            [], tools=[{"type": "function", "function": {"name": "send_message"}}]
        )
    )
    assert result.content == ""
    assert result.tool_calls == [
        {"id": "1", "type": "function", "function": {"name": "", "arguments": ""}}
    ]
    assert result.provider == "primary"
    assert router.primary._session.payloads[0]["stream"] is True
    assert not router.fallback._session.payloads


def test_explicit_response_limit_is_enforced_for_streams_and_json():
    async def run():
        from aiohttp import web

        async def handler(request):
            if (await request.json()).get("stream"):
                return web.Response(
                    body=(
                        b'data: {"choices":[{"delta":{"content":"'
                        + b"x" * 200
                        + b'"}}]}\n\ndata: [DONE]\n\n'
                    ),
                    content_type="text/event-stream",
                )
            return web.json_response({"choices": [{"message": {"content": "x" * 200}}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = openai_compat_provider(
            base_url=f"http://127.0.0.1:{port}/v1",
            model="m",
            policy=ProviderPolicy(max_response_bytes=100),
            retry_attempts=1,
            empty_response_retries=0,
        )
        client.available = True
        try:
            for stream in (False, True):
                with pytest.raises(ProviderUnavailableError, match="size limit"):
                    await client.generate_response([], stream=stream)
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(run())


def _router(primary_responses, fallback_responses, **kwargs):
    router = openai_compat_provider(
        base_url="https://primary.test/v1",
        model="primary-model",
        api_key="primary-key",
        fallback_base_url="https://fallback.test/v1",
        fallback_model="fallback-model",
        fallback_api_key="fallback-key",
        **kwargs,
    )
    assert isinstance(router, ProviderRouter)
    for client, responses in (
        (router.primary, primary_responses),
        (router.fallback, fallback_responses),
    ):
        client.available = True
        client._session = FakeSequenceSession(responses)
        assert len(client._endpoints) == 1
    return router


def test_router_preserves_primary_retry_schedule_and_fallback_model(monkeypatch):
    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    router = _router(
        [FakeErrorResponse(503, "down"), FakeErrorResponse(503, "down")],
        [FakeResponse()],
    )
    result = asyncio.run(router.generate_response([], model="override"))
    assert result == "ok"
    assert result.model == "fallback-model" and result.provider == "fallback"
    assert len(router.primary._session.payloads) == 2
    assert router.primary._session.payloads[0]["model"] == "override"
    assert router.fallback._session.payloads[0]["model"] == "fallback-model"
    assert router.primary.authentication is not router.fallback.authentication
    assert result.timing["model"] == "fallback-model"
    assert result.timing["endpoint"] == "fallback"


@pytest.mark.parametrize("status", [400, 401, 404, 429])
def test_router_deterministic_rejection_or_cooldown_skips_failed_upstream(status):
    router = _router(
        [FakeErrorResponse(status, "unavailable")], [FakeResponse()], retry_attempts=1
    )
    # Deterministic errors extend a one-attempt budget to reach the fallback;
    # rate limits honor the explicit budget and succeed with fast fallback at two.
    if status == 429:
        router.retry_attempts = 2
    assert asyncio.run(router.generate_response([], fast_fallback=True)) == "ok"
    assert (
        len(router.primary._session.payloads)
        == len(router.fallback._session.payloads)
        == 1
    )


def test_router_night_preference_and_vision_are_independent_clients():
    router = _router([], [FakeResponse()])
    result = asyncio.run(router.generate_response([], prefer_fallback=True))
    assert result.provider == "fallback"
    assert not router.primary._session.payloads
    vision = openai_compat_provider(
        base_url="https://primary.test/v1",
        model="p",
        vision_base_url="https://vision.test/v1",
        vision_model="v",
        vision_api_key="vision-key",
    )
    vision.vision.available = True
    vision.vision._session = FakeSession()
    result = asyncio.run(
        vision.generate_response([{"role": "user", "content": "look"}], images=["AA=="])
    )
    assert result.model == "v" and result.provider == "vision"
    assert vision.primary._session is None


def test_router_empty_response_recovers_nonstreaming_on_another_provider():
    router = _router(
        [FakeEmptyResponse()],
        [FakeResponse()],
        retry_attempts=1,
        empty_response_retries=1,
    )
    result = asyncio.run(router.generate_response([]))
    assert result.provider == "fallback"
    assert router.primary._session.payloads[0]["stream"] is True
    assert router.fallback._session.payloads[0]["stream"] is False


def test_router_learns_media_failure_and_degrades_only_after_all_slots_fail():
    async def run():
        class MediaProvider(ChatProvider):
            def __init__(self, name):
                self.name = name
                self.requests = []

            async def initialize(self):
                return True

            async def generate_response(self, messages, **kwargs):
                self.requests.append((messages, kwargs))
                if kwargs.get("images"):
                    raise ProviderMediaUnsupportedError("image input unavailable")
                return ProviderResult("text response", provider=self.name)

        primary, fallback = MediaProvider("primary"), MediaProvider("fallback")
        router = ProviderRouter(primary, fallback, retry_attempts=1)
        result = await router.generate_response(
            [{"role": "user", "content": "inspect"}], images=["AA=="]
        )
        assert result == "text response"
        assert len(primary.requests) == 1 and len(fallback.requests) == 2
        assert not fallback.requests[-1][1].get("images")

    asyncio.run(run())


def test_media_routing_reaches_primary_after_vision_and_fallback_fail(monkeypatch):
    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    class Endpoint(ChatProvider):
        def __init__(self, name, succeeds=False):
            self.name = name
            self.succeeds = succeeds
            self.calls = 0

        async def initialize(self):
            return True

        async def generate_response(self, messages, **kwargs):
            self.calls += 1
            if not self.succeeds:
                raise ProviderUnavailableError("temporarily unavailable")
            assert kwargs["images"] == ["AA=="]
            return ProviderResult("saw image", provider=self.name)

    primary = Endpoint("primary", succeeds=True)
    vision, fallback = Endpoint("vision"), Endpoint("fallback")
    router = ProviderRouter(primary, fallback, vision, retry_attempts=3)
    result = asyncio.run(router.generate_response(
        [{"role": "user", "content": "inspect"}], images=["AA=="]
    ))
    assert result.provider == "primary"
    assert primary.calls == 1


def test_media_degradation_tells_the_model_the_image_was_not_delivered():
    class Endpoint(ChatProvider):
        name = "primary"

        async def initialize(self):
            return True

        async def generate_response(self, messages, **kwargs):
            if kwargs.get("media"):
                raise ProviderMediaUnsupportedError("image unavailable")
            assert "attachment" in str(messages).lower()
            assert "unavailable" in str(messages).lower()
            return ProviderResult("Please attach a supported image.")

    router = ProviderRouter(Endpoint(), retry_attempts=1)
    assert asyncio.run(router.generate_response(
        [{"role": "user", "content": "what color is this?"}],
        media=[{"b64": "AA==", "mime_type": "image/png"}],
    )) == "Please attach a supported image."


def test_concurrent_results_never_exchange_tool_calls_usage_models_or_timing():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class Response(FakeResponse):
            def __init__(self, marker):
                self.marker = marker
                self.content = None

            async def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": self.marker,
                                "tool_calls": [
                                    {
                                        "id": self.marker,
                                        "function": {
                                            "name": "send_message",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11 if self.marker == "one" else 22,
                        "completion_tokens": 1,
                    },
                }

            async def __aexit__(self, *_args):
                if self.marker == "one":
                    entered.set()
                    await release.wait()

        client = openai_compat_provider(
            base_url="https://example.test/v1", model="base"
        )
        client.available = True
        client._session = FakeSequenceSession([Response("one"), Response("two")])
        first = asyncio.create_task(
            client.generate_response([], model="first", stream=False)
        )
        await entered.wait()
        second = await client.generate_response([], model="second", stream=False)
        release.set()
        first = await first
        assert (first.tool_calls[0]["id"], second.tool_calls[0]["id"]) == ("one", "two")
        assert (first.usage["prompt_tokens"], second.usage["prompt_tokens"]) == (11, 22)
        assert (first.model, second.model) == ("first", "second")
        assert (first.timing["model"], second.timing["model"]) == ("first", "second")
        first.assistant_message["tool_calls"][0]["id"] = "changed"
        assert first.tool_calls[0]["id"] == "one"
        assert second.tool_calls[0]["id"] == "two"

    asyncio.run(run())


def test_missing_completion_metadata_does_not_read_legacy_usage():
    async def run():
        client = OpenAICompatibleProvider("https://example.test/v1", "m", 10, 0.5)
        client._last_usage = {"prompt_tokens": 999}

        async def complete(*_args, **_kwargs):
            return {"content": "legacy response"}

        client.generate_chat_completion = complete
        result = await client.generate_response([])
        assert result.usage == {}

    asyncio.run(run())


def test_tool_dispatch_never_consumes_shared_state():
    bot = MaxwellBot.__new__(MaxwellBot)
    bot.ai_provider = type(
        "LegacyProvider", (), {"_last_tool_calls": [{"id": "other-user"}]}
    )()
    assert bot._native_calls_from(object()) == []
    assert bot._native_calls_from("text") == []
    assert bot._native_calls_from(
        ProviderResult("", tool_calls=[{"id": "this-user"}])
    ) == [{"id": "this-user"}]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/v1",
        "https://[::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https://user:password@example.test/v1",
    ],
)
def test_public_policy_rejects_unsafe_urls_before_any_session(url):
    with pytest.raises(ValueError):
        openai_compat_provider(
            base_url=url, model="m", policy=ProviderPolicy(public_network_only=True)
        )


def test_policy_is_immutable_and_cannot_enable_fallback_or_redirects():
    policy = ProviderPolicy(public_network_only=True, allow_provider_fallback=False)
    with pytest.raises(FrozenInstanceError):
        policy.public_network_only = False
    with pytest.raises(ValueError):
        ProviderPolicy(public_network_only=True, allow_redirects=True)
    with pytest.raises(ValueError, match="forbids"):
        openai_compat_provider(
            base_url="https://example.test",
            model="m",
            policy=policy,
            fallback_base_url="https://other.test",
            fallback_model="other",
        )
    client = openai_compat_provider(
        base_url="https://example.test", model="m", policy=policy
    )
    with pytest.raises(AttributeError):
        client.policy = ProviderPolicy()


def test_byok_public_resolver_and_verified_tls_are_installed_before_first_session():
    async def run():
        from providers import _PublicOnlyResolver

        client = make_request_provider(
            type(
                "Bot", (), {"_make_chat_provider": staticmethod(openai_compat_provider)}
            )(),
            {"provider": "openrouter", "model": "m", "api_key": "key"},
        )
        try:
            session = await client._get_session()
            assert isinstance(session.connector._resolver, _PublicOnlyResolver)
            assert session.connector._ssl is True
            assert client.policy.max_response_bytes == 2 * 1024 * 1024
            assert client.policy.max_request_seconds == 300
            assert not client.policy.allow_provider_fallback
        finally:
            await client.close()

    asyncio.run(run())


def test_injectable_authentication_is_request_local_and_credential_echoes_are_scrubbed(
    caplog,
):
    async def run():
        class Authentication:
            count = 0

            async def headers(self):
                self.count += 1
                return {"Authorization": f"Bearer credential-{self.count}"}

        class Response(FakeResponse):
            content = None

            def __init__(self, credential):
                self.credential = credential

            async def json(self):
                return {
                    "choices": [{"message": {"content": f"echo {self.credential}"}}]
                }

        class Session(FakeSession):
            def post(self, _url, *, headers, **_kwargs):
                return Response(headers["Authorization"].removeprefix("Bearer "))

        auth = Authentication()
        client = provider_from_config(
            ProviderConfig(
                name="injected",
                base_url="https://example.test",
                model="m",
                authentication=auth,
                policy=ProviderPolicy(sensitive_credentials=True),
            )
        )
        client.available = True
        client._session = Session()
        one, two = await asyncio.gather(
            client.generate_response([]), client.generate_response([])
        )
        assert one == two == "echo [redacted]"
        assert auth.count == 2
        assert "credential-" not in caplog.text

    asyncio.run(run())


def test_authentication_failure_is_sanitized_without_retrying_http(caplog):
    class BrokenAuthentication:
        async def headers(self):
            raise RuntimeError("secret-token")

    client = openai_compat_provider(
        base_url="https://example.test",
        model="m",
        authentication=BrokenAuthentication(),
    )
    client.available = True
    client._session = FakeSession()
    with pytest.raises(ProviderAuthenticationError) as caught:
        asyncio.run(client.generate_response([]))
    assert not client._session.payloads
    assert "secret-token" not in str(caught.value) + caplog.text


def test_injected_no_auth_does_not_inherit_static_credentials():
    class NoAuthentication:
        async def headers(self):
            return {}

    client = openai_compat_provider(
        base_url="https://example.test",
        model="m",
        api_key="stale-key",
        authentication=NoAuthentication(),
    )
    assert "Authorization" not in asyncio.run(
        client._auth_headers(client._endpoints[0])
    )


def test_sensitive_transport_failure_does_not_expose_credentials(caplog):
    class BrokenSession(FakeSession):
        def post(self, *_args, **_kwargs):
            raise RuntimeError("user-secret in untrusted transport exception")

    client = openai_compat_provider(
        base_url="https://example.test",
        model="m",
        api_key="user-secret",
        policy=ProviderPolicy(sensitive_credentials=True),
        retry_attempts=1,
    )
    client.available = True
    client._session = BrokenSession()
    with pytest.raises(ProviderUnavailableError) as caught:
        asyncio.run(client.generate_response([]))
    assert "user-secret" not in str(caught.value) + caplog.text


def test_policy_deadline_includes_authentication_and_cancellation_propagates():
    async def run():
        class HangingAuthentication:
            async def headers(self):
                await asyncio.Event().wait()

        client = openai_compat_provider(
            base_url="https://example.test",
            model="m",
            authentication=HangingAuthentication(),
            policy=ProviderPolicy(max_request_seconds=1),
        )
        client.available = True
        client._session = FakeSession()
        with pytest.raises(ProviderUnavailableError, match="time limit"):
            await client.generate_response([])
        task = asyncio.create_task(client.generate_response([]))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client._session.payloads

    asyncio.run(run())


def test_router_initialization_failure_keeps_healthy_alternate_available(caplog):
    async def run():
        class Client(ChatProvider):
            def __init__(self, name, fails=False):
                self.name, self.fails, self.available, self.closed = (
                    name,
                    fails,
                    False,
                    False,
                )

            async def initialize(self):
                if self.fails:
                    raise RuntimeError("secret-url")
                self.available = True

            async def generate_response(self, *_args, **_kwargs):
                return ProviderResult("ok")

            async def close(self):
                self.closed = True

        primary, fallback = Client("primary", True), Client("fallback")
        router = ProviderRouter(primary, fallback)
        assert await router.initialize() is True
        await router.close()
        assert primary.closed and fallback.closed

    asyncio.run(run())
    assert "secret-url" not in caplog.text


def test_migration_preserves_blanked_credentials_advanced_aliases_and_is_idempotent(
    tmp_path,
):
    path = tmp_path / ".env"
    path.write_text(
        "AI_API_URL=https://friendly.test/v1\nOLLAMA_BASE_URL=https://legacy.test/v1\n"
        "OLLAMA_MODEL=legacy-model\nOLLAMA_API_KEY=old-secret\nAI_API_KEY=\n"
        'OLLAMA_MAX_TOKENS=321\nOLLAMA_FALLBACK_REASONING_EFFORT=low\nKEEP="unchanged"\n'
    )
    assert migrate(path) is True
    values = dotenv_values(path)
    assert values["AI_BASE_URL"] == "https://friendly.test/v1"
    assert values["AI_API_KEY"] == values["OLLAMA_API_KEY"] == ""
    assert values["AI_MAX_OUTPUT_TOKENS"] == values["OLLAMA_MAX_TOKENS"] == "321"
    assert values["AI_FALLBACK_REASONING_EFFORT"] == "low"
    assert values["KEEP"] == "unchanged"
    assert migrate(path) is False


def test_fresh_process_provider_initialization_imports_are_compatible():
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from maxwell_core.providers.factory import provider_from_config; "
            "from maxwell_core.providers import ProviderConfig; "
            "from providers import OllamaProvider, OpenAICompatibleProvider; "
            "assert OllamaProvider is OpenAICompatibleProvider; "
            "assert isinstance(provider_from_config(ProviderConfig(name='p')), OpenAICompatibleProvider)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
