"""Provider redaction: real credentials are scrubbed, the session id is not."""

from __future__ import annotations

import asyncio

import pytest

import providers as providers_mod
from maxwell_core.providers.models import ProviderPolicy
from providers import OpenAICompatibleProvider

KEY = "sk-test-placeholder-123"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def instant(*_a, **_k):
        return None

    monkeypatch.setattr(providers_mod.asyncio, "sleep", instant)
    for name in ("OPENCODE_SESSION", "AI_OPENCODE_SESSION", "OLLAMA_OPENCODE_SESSION"):
        monkeypatch.delenv(name, raising=False)


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status = status
        self._body = body
        self._text = text
        self.content = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class Session:
    closed = False

    def __init__(self, *responses):
        self.queue = list(responses)
        self.headers = []

    def post(self, url, json=None, headers=None, **kwargs):
        self.headers.append(dict(headers or {}))
        return self.queue.pop(0)

    async def close(self):
        self.closed = True


class HeaderAuth:
    def __init__(self, headers):
        self._headers = headers

    async def headers(self):
        return dict(self._headers)


def _provider(session, *, byok, **kwargs):
    kwargs.setdefault("api_key", KEY)
    p = OpenAICompatibleProvider(
        "https://opencode.ai/zen/v1",
        "m",
        512,
        retry_attempts=1,
        empty_response_retries=0,
        policy=ProviderPolicy(sensitive_credentials=byok),
        **kwargs,
    )
    p._session = session
    p.available = True
    return p


def _chat(p):
    return asyncio.run(
        p.generate_chat_completion([{"role": "user", "content": "ping"}], stream=False)
    )


def _reply(text):
    return Resp(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


def test_session_header_is_sent_but_not_a_secret():
    headers = {"Authorization": f"Bearer {KEY}", "x-opencode-session": "maxwell"}
    assert OpenAICompatibleProvider._credential_secrets(headers) == (KEY,)
    assert OpenAICompatibleProvider._credential_secrets(
        {"X-OpenCode-Session": "maxwell", "x-api-key": "other-key", "e": ""}
    ) == ("other-key",)


def test_byok_reply_keeps_ordinary_words_and_redacts_the_key():
    session = Session(_reply(f"maxwell says hi; echoed {KEY}"))
    result = _chat(_provider(session, byok=True))
    assert session.headers[0]["x-opencode-session"] == "maxwell"
    assert result["content"] == "maxwell says hi; echoed [redacted]"


def test_byok_custom_auth_header_values_are_still_redacted():
    auth = HeaderAuth({"x-api-key": "custom-secret-456"})
    session = Session(_reply("leak custom-secret-456 and maxwell"))
    result = _chat(_provider(session, byok=True, authentication=auth))
    assert result["content"] == "leak [redacted] and maxwell"


def test_error_text_keeps_session_word_and_redacts_key():
    body = f"maxwell request rejected: bad key {KEY}"
    session = Session(Resp(400, text=body))
    with pytest.raises(Exception) as info:
        _chat(_provider(session, byok=False))
    text = str(info.value)
    assert KEY not in text
    assert "maxwell request rejected" in text
