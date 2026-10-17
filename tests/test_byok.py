"""BYOK encryption, request isolation, endpoint validation, and redaction."""

from __future__ import annotations

import asyncio
import logging
import socket
from types import SimpleNamespace

import pytest

from bot import MaxwellBot
from plugins.maxwell_extras.byok import (
    CredentialVault,
    VaultUnavailable,
    make_request_provider,
    parse_model_support,
)
from providers import (
    OpenAICompatibleProvider,
    ProviderRequestError,
    _PublicOnlyResolver,
)


KEY_A = "11" * 32
KEY_B = "22" * 32
API_KEY = "sk-user-owned-credential-123456"


def test_saved_connection_replaces_the_model_for_normal_chat(tmp_path):
    vault = CredentialVault(tmp_path / "byok.sqlite3", KEY_A)
    vault.save(
        "123",
        "custom",
        "labs-leanstral-1-5-1",
        API_KEY,
        base_url="https://api.mistral.ai/v1",
    )

    def factory(*, name, **kwargs):
        client = OpenAICompatibleProvider(**kwargs)
        client.name = name
        return client

    bot = object.__new__(MaxwellBot)
    bot._byok_vault = vault
    bot._make_chat_provider = factory
    chat = SimpleNamespace(
        author=SimpleNamespace(id="123", bot=False), user_install=False
    )
    provider = asyncio.run(MaxwellBot._personal_provider(bot, chat))
    try:
        assert provider is not None
        assert provider.model == "labs-leanstral-1-5-1"
        assert provider.base_url == "https://api.mistral.ai/v1"
    finally:
        asyncio.run(provider.close())

    stranger = SimpleNamespace(author=SimpleNamespace(id="999", bot=False))
    assert asyncio.run(MaxwellBot._personal_provider(bot, stranger)) is None
    bot_author = SimpleNamespace(author=SimpleNamespace(id="123", bot=True))
    assert asyncio.run(MaxwellBot._personal_provider(bot, bot_author)) is None


def test_vault_encrypts_masks_rotates_and_deletes(tmp_path):
    path = tmp_path / "private" / "byok.sqlite3"
    vault = CredentialVault(path, KEY_A)
    vault.save(
        "123", "custom", "gpt-4.1-mini", API_KEY, base_url="https://api.example.com/v1"
    )

    assert API_KEY.encode() not in path.read_bytes()
    assert vault.get("123")["api_key"] == API_KEY
    assert vault.status("123") == {
        "provider": "custom",
        "provider_label": "Custom API endpoint",
        "model": "gpt-4.1-mini",
        "masked_key": "••••3456",
        "base_url": "https://api.example.com/v1",
        "modalities": "text, vision, tools",
        "generation": "reasoning=off max_tokens=4096 timeout=300 turn_timeout=900",
    }
    assert path.stat().st_mode & 0o777 == 0o600

    vault.rotate_master_key(KEY_B)
    assert vault.get("123")["api_key"] == API_KEY
    with pytest.raises(VaultUnavailable):
        CredentialVault(path, KEY_A).get("123")

    assert vault.delete("123") is True
    assert vault.delete("123") is False
    assert vault.get("123") is None
    assert API_KEY.encode() not in path.read_bytes()


def test_vault_binds_ciphertext_to_owner_and_provider(tmp_path):
    path = tmp_path / "byok.sqlite3"
    vault = CredentialVault(path, KEY_A)
    vault.save(
        "123",
        "custom",
        "openai/gpt-oss-120b",
        API_KEY,
        base_url="https://api.example.com/v1",
    )
    with vault._db() as db:
        db.execute("UPDATE credentials SET user_id='456'")
    with pytest.raises(VaultUnavailable):
        vault.get("456")


def test_vault_fails_closed_without_encryption_key(tmp_path, monkeypatch):
    monkeypatch.delenv("MAXWELL_BYOK_ENCRYPTION_KEY", raising=False)
    vault = CredentialVault(tmp_path / "byok.sqlite3", "")
    assert vault.enabled is False
    with pytest.raises(VaultUnavailable):
        vault.save(
            "123",
            "custom",
            "gpt-4.1-mini",
            API_KEY,
            base_url="https://api.example.com/v1",
        )


def test_provider_registry_builds_separate_single_credential_clients():
    made = []

    def factory(*, name, **kwargs):
        made.append(kwargs)
        client = OpenAICompatibleProvider(**kwargs)
        client.name = name
        return client

    main = OpenAICompatibleProvider(
        "https://main.example/v1", "main", 100, 0.4, api_key="main-key"
    )
    bot = SimpleNamespace(_make_chat_provider=factory, ai_provider=main)
    one = make_request_provider(
        bot,
        {
            "provider": "custom",
            "model": "gpt-4.1-mini",
            "api_key": "user-one-key",
            "base_url": "https://api.example.com/v1",
        },
    )
    two = make_request_provider(
        bot,
        {
            "provider": "custom",
            "model": "openai/gpt-oss-120b",
            "api_key": "user-two-key",
            "base_url": "https://second.example.com/v1",
        },
    )

    assert one is not two
    assert [row["base_url"] for row in made] == [
        "https://api.example.com/v1",
        "https://second.example.com/v1",
    ]
    assert [row["api_key"] for row in made] == ["user-one-key", "user-two-key"]
    assert len(one._endpoints) == len(two._endpoints) == 1
    assert one.policy.public_network_only and two.policy.public_network_only
    assert main.api_key == "main-key"
    assert main.base_url == "https://main.example/v1"


def test_failed_byok_request_does_not_fall_back_to_main_provider():
    class MainProvider:
        calls = 0

        async def generate_response(self, *_args, **_kwargs):
            self.calls += 1
            return "should not be used"

    class UserProvider:
        async def generate_response(self, *_args, **_kwargs):
            raise RuntimeError("user provider failed")

    main = MainProvider()
    bot = SimpleNamespace(ai_provider=main, _control={})
    with pytest.raises(RuntimeError, match="user provider failed"):
        asyncio.run(
            MaxwellBot._generate_response(
                bot,
                [{"role": "user", "content": "private request"}],
                provider=UserProvider(),
            )
        )
    assert main.calls == 0


def test_public_resolver_rejects_private_literals_and_pins_public_dns(monkeypatch):
    resolver = _PublicOnlyResolver()

    async def check_literals():
        for address in (
            "127.0.0.1",
            "169.254.169.254",
            "::1",
            "fc00::1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
            "ff02::1",
            "fec0::1",
            "64:ff9b::7f00:1",
        ):
            with pytest.raises(OSError):
                await resolver.resolve(address, 443)
        result = await resolver.resolve("1.1.1.1", 443)
        assert result[0]["host"] == "1.1.1.1"
        assert result[0]["flags"] & socket.AI_NUMERICHOST

    asyncio.run(check_literals())

    class MixedDns:
        async def getaddrinfo(self, host, port, *, family, type):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
            ]

    monkeypatch.setattr("providers.asyncio.get_running_loop", lambda: MixedDns())
    with pytest.raises(OSError):
        asyncio.run(resolver.resolve("provider.example", 443))


class _Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.content = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return self.body

    async def json(self):
        import json

        return json.loads(self.body)


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def post(self, url, **options):
        self.calls.append((url, options))
        return self.response


def _provider():
    def factory(*, name, **kwargs):
        client = OpenAICompatibleProvider(**kwargs)
        client.name = name
        return client

    bot = SimpleNamespace(_make_chat_provider=factory)
    client = make_request_provider(
        bot,
        {
            "provider": "custom",
            "model": "gpt-4.1-mini",
            "api_key": API_KEY,
            "base_url": "https://api.example.com/v1",
        },
    )
    return client


def test_custom_endpoint_stores_model_support_and_rejects_private_urls(tmp_path):
    from maxwell_core.providers.factory import openai_compat_provider

    settings = parse_model_support(
        "vision, audio",
        "reasoning=on max_tokens=8192 effort=low context=32000",
    )
    vault = CredentialVault(tmp_path / "byok.sqlite3", KEY_A)
    vault.save(
        "123",
        "custom",
        "local-model",
        API_KEY,
        base_url="https://models.example",
        settings=settings,
    )
    stored = vault.get("123")
    assert stored["base_url"] == "https://models.example/v1"
    assert stored["settings"]["tools"] is False
    assert stored["settings"]["context"] == 32000
    assert API_KEY.encode() not in (tmp_path / "byok.sqlite3").read_bytes()
    status = vault.status("123")
    assert status["provider_label"] == "Custom API endpoint"
    assert status["base_url"] == "https://models.example/v1"
    assert status["modalities"] == "text, vision, audio"
    assert "reasoning=on" in status["generation"]
    assert "context=32000" in status["generation"]

    bot = SimpleNamespace(_make_chat_provider=openai_compat_provider)
    client = make_request_provider(bot, stored)
    assert client.base_url == "https://models.example/v1"
    assert client.model == "local-model"
    assert client.max_tokens == 8192
    assert client.capabilities.vision is True
    assert client.capabilities.audio is True
    assert client.capabilities.native_tools is False
    assert client.capabilities.reasoning is True
    assert client.capabilities.context_window == 32000
    assert client.enable_audio_input is True
    assert client._endpoints[0].disable_reasoning is False
    assert client._endpoints[0].reasoning_effort == "low"
    assert client.policy.max_output_tokens == 8192
    assert client.policy.public_network_only is True
    assert len(client._endpoints) == 1

    for blocked in (
        "http://models.example/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.8/v1",
        "https://localhost/v1",
        "https://gateway.local/v1",
        "https://user:secret@models.example/v1",
        "https://models.example/v1?key=secret",
        "",
    ):
        with pytest.raises(ValueError):
            vault.save(
                "123",
                "custom",
                "local-model",
                API_KEY,
                base_url=blocked,
                settings=settings,
            )


@pytest.mark.parametrize(
    "legacy,url",
    [
        ("openai", "https://api.openai.com/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("groq", "https://api.groq.com/openai/v1"),
    ],
)
def test_legacy_credential_migrates_without_changing_destination(tmp_path, legacy, url):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    vault = CredentialVault(tmp_path / "byok.sqlite3", KEY_A)
    nonce = b"123456789012"
    encrypted = AESGCM(bytes.fromhex(KEY_A)).encrypt(
        nonce, API_KEY.encode(), vault._aad("123", legacy)
    )
    with vault._db() as db:
        db.execute(
            "INSERT INTO credentials(user_id,provider,model,nonce,ciphertext,updated_at) VALUES(?,?,?,?,?,?)",
            ("123", legacy, "my-model", nonce, encrypted, 1),
        )
    stored = vault.get("123")
    assert stored["provider"] == "custom"
    assert stored["base_url"] == url
    assert stored["api_key"] == API_KEY
    assert stored["settings"] is None
    assert vault.get("123") == stored
    with vault._db() as db:
        row = db.execute("SELECT provider,nonce FROM credentials").fetchone()
    assert row[0] == "custom" and row[1] != nonce
    with pytest.raises(ValueError, match="supported provider"):
        vault.save("123", legacy, "my-model", API_KEY)


def test_byok_context_window_shrinks_the_prompt_budget():
    from bot import _current_request_provider

    provider = SimpleNamespace(
        capabilities=SimpleNamespace(context_window=2048),
        max_tokens=4096,
    )
    token = _current_request_provider.set(provider)
    try:
        budget = MaxwellBot._prompt_budget_chars(SimpleNamespace(_control={}))
    finally:
        _current_request_provider.reset(token)
    assert budget < 10000


def test_configured_modalities_gate_images_and_tools():
    import json

    def client_for(modalities: str):
        settings = parse_model_support(modalities, "reasoning=off max_tokens=128")
        client = make_request_provider(
            SimpleNamespace(
                _make_chat_provider=lambda *, name, **kwargs: OpenAICompatibleProvider(
                    **kwargs
                )
            ),
            {
                "provider": "custom",
                "model": "local-model",
                "api_key": API_KEY,
                "base_url": "https://models.example/v1",
                "settings": settings,
            },
        )
        client.name = "byok:custom"
        session = _Session(
            _Response(
                200,
                json.dumps(
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": "OK"}}
                        ],
                    }
                ),
            )
        )
        client._session = session
        return client, session

    text_client, text_session = client_for("tools")
    asyncio.run(
        text_client.generate_chat_completion(
            [{"role": "user", "content": "hi"}],
            media=[{"b64": "abcd", "mime_type": "image/png"}],
            tools=[{"type": "function", "function": {"name": "send_message"}}],
        )
    )
    text_body = text_session.calls[0][1]["json"]
    assert "image_url" not in json.dumps(text_body)
    assert text_body["tools"][0]["function"]["name"] == "send_message"

    vision_client, vision_session = client_for("vision")
    asyncio.run(
        vision_client.generate_chat_completion(
            [{"role": "user", "content": "hi"}],
            media=[{"b64": "abcd", "mime_type": "image/png"}],
            tools=[{"type": "function", "function": {"name": "send_message"}}],
        )
    )
    vision_body = vision_session.calls[0][1]["json"]
    assert "image_url" in json.dumps(vision_body)
    assert "tools" not in vision_body


def test_byok_rejects_redirects_and_sanitizes_provider_errors(caplog):
    client = _provider()
    session = _Session(_Response(302, f"redirect token={API_KEY}"))
    client._session = session
    with caplog.at_level(logging.WARNING, logger="providers"):
        with pytest.raises(ProviderRequestError) as exc:
            asyncio.run(
                client.generate_chat_completion([{"role": "user", "content": "hi"}])
            )

    assert "api.example.com" in session.calls[0][0]
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in str(exc.value)
    assert API_KEY not in caplog.text
    assert len(session.calls) == 1


def test_byok_scrubs_provider_echoes_before_return_or_logging(caplog):
    import json

    client = _provider()
    body = json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": f"echo {API_KEY}"}}
            ],
        }
    )
    session = _Session(_Response(200, body))
    client._session = session
    with caplog.at_level(logging.INFO, logger="providers"):
        result = asyncio.run(
            client.generate_chat_completion([{"role": "user", "content": "hi"}])
        )
    assert API_KEY not in result["content"]
    assert "[redacted]" in result["content"]
    assert API_KEY not in caplog.text
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_reasoning_levels_and_slow_model_settings_are_durable(tmp_path, effort):
    settings = parse_model_support(
        "tools",
        f"reasoning=on effort={effort} timeout=1200 turn_timeout=2400 context=128000",
    )
    path = tmp_path / "byok.sqlite3"
    vault = CredentialVault(path, KEY_A)
    vault.save(
        "123",
        "custom",
        "reasoning-model",
        API_KEY,
        base_url="https://model.example/v1",
        settings=settings,
    )
    stored = CredentialVault(path, KEY_A).get("123")
    assert stored["settings"] == settings
    from maxwell_core.providers.factory import openai_compat_provider

    client = make_request_provider(
        SimpleNamespace(_make_chat_provider=openai_compat_provider), stored
    )
    assert client.policy.max_request_seconds == 1200
    assert client.turn_timeout_seconds == 2400
    assert client.capabilities.context_window == 128000
    assert client._endpoints[0].reasoning_effort == ("" if effort == "none" else effort)
    assert client._endpoints[0].disable_reasoning is (effort == "none")
    assert parse_model_support("tools", "reasoning=off effort=high")["effort"] == ""


@pytest.mark.parametrize(
    "settings",
    [
        "timeout=0",
        "timeout=1801",
        "timeout=NaN",
        "turn_timeout=7201",
        "timeout=1200 turn_timeout=600",
        "context=100",
        "effort=ultra",
    ],
)
def test_invalid_limits_and_levels_fail_closed(settings):
    with pytest.raises(ValueError):
        parse_model_support("tools", settings)


def test_provider_limits_separate_whole_task_from_llm_deadline():
    from provider_limits import request_timeout_seconds, turn_timeout_seconds

    bot = SimpleNamespace(
        config=SimpleNamespace(
            AI_REQUEST_TIMEOUT_SECONDS=600, AI_TURN_TIMEOUT_SECONDS=1800
        ),
        _control={},
    )
    assert request_timeout_seconds(bot) == 600
    assert turn_timeout_seconds(bot) == 1800
    personal = SimpleNamespace(
        policy=SimpleNamespace(max_request_seconds=1200), turn_timeout_seconds=2400
    )
    assert request_timeout_seconds(bot, personal) == 1200
    assert turn_timeout_seconds(bot, personal) == 2400
    bot._control["ai_timeout_seconds"] = "nan"
    assert request_timeout_seconds(bot) == 3600


def test_legacy_migration_never_recreates_a_concurrently_deleted_key(tmp_path):
    from contextlib import contextmanager
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    vault = CredentialVault(tmp_path / "byok.sqlite3", KEY_A)
    nonce = b"123456789012"
    encrypted = AESGCM(bytes.fromhex(KEY_A)).encrypt(
        nonce, API_KEY.encode(), vault._aad("123", "groq")
    )
    with vault._db() as db:
        db.execute(
            "INSERT INTO credentials(user_id,provider,model,nonce,ciphertext,updated_at,base_url) VALUES(?,?,?,?,?,?,?)",
            (
                "123",
                "groq",
                "my-model",
                nonce,
                encrypted,
                1,
                "https://chosen.example/v1",
            ),
        )
    original = vault._db
    count = 0

    @contextmanager
    def concurrent_delete():
        nonlocal count
        count += 1
        with original() as db:
            if count == 2:
                db.execute("DELETE FROM credentials WHERE user_id='123'")
            yield db

    vault._db = concurrent_delete
    assert vault.get("123") is None
    assert not vault.has_credential("123")
