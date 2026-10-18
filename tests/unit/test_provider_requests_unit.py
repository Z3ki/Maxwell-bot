"""OpenAI-compatible provider: request building and error handling, offline.

Every network call goes through a fake aiohttp session; nothing leaves the box.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json

import pytest

import providers as providers_mod
from maxwell_core.providers import error_rules as rules
from maxwell_core.providers.errors import (
    ProviderAuthenticationError,
    ProviderEmptyResponseError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderUsageExhaustedError,
)
from maxwell_core.providers.http import (
    _PublicOnlyResolver,
    _ip_is_public,
    _read_json_response_limited,
    _read_response_text_limited,
    normalize_base_url,
)
from maxwell_core.providers.models import ProviderPolicy
from maxwell_core.providers.tool_calls import (
    _append_tool_call_arguments,
    _extract_partial_reasoning,
    _keep_tool_call_provider_fields,
)
from providers import OpenAICompatibleProvider, _strip_media_parts


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_sleep_no_env(monkeypatch):
    async def instant(*_a, **_k):
        return None

    monkeypatch.setattr(providers_mod.asyncio, "sleep", instant)
    for name in (
        "AI_EMPTY_RESPONSE_RETRIES",
        "OLLAMA_EMPTY_RESPONSE_RETRIES",
        "AI_ENDPOINT_COOLDOWN_SECONDS",
        "OLLAMA_ENDPOINT_COOLDOWN_SECONDS",
        "OPENCODE_SESSION",
        "AI_OPENCODE_SESSION",
        "OLLAMA_OPENCODE_SESSION",
    ):
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


def ok(content="hi", **message):
    return Resp(
        200,
        {
            "choices": [
                {"message": {"role": "assistant", "content": content, **message}}
            ]
        },
    )


class Session:
    """Replays queued responses and records every request."""

    closed = False

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []

    def post(self, url, json=None, headers=None, **kwargs):
        self.requests.append({"url": url, "json": json, "headers": headers or {}})
        if not self.queue:
            raise AssertionError("unexpected extra request")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, headers=None, **kwargs):
        self.requests.append({"url": url, "get": True, "headers": headers or {}})
        return self.queue.pop(0) if self.queue else Resp(200)

    async def close(self):
        self.closed = True


def provider(session=None, *, available=True, **kwargs):
    kwargs.setdefault("retry_attempts", 3)
    kwargs.setdefault("empty_response_retries", 0)
    p = OpenAICompatibleProvider("http://primary.test/v1", "main-model", 512, **kwargs)
    p._session = session or Session()
    p.available = available
    return p


def chat(p, **kwargs):
    kwargs.setdefault("stream", False)
    return run(
        p.generate_chat_completion([{"role": "user", "content": "ping"}], **kwargs)
    )


# --------------------------------------------------------------------------
# construction and endpoints
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://localhost:11434", "http://localhost:11434/v1"),
        ("https://api.example.test/", "https://api.example.test/v1"),
        ("https://api.example.test/v2", "https://api.example.test/v2"),
        ("https://h.test/api/v1/", "https://h.test/api/v1"),
        ("localhost:11434", "localhost:11434/v1"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


def test_constructor_builds_primary_fallback_and_vision_endpoints():
    p = OpenAICompatibleProvider(
        "http://primary.test",
        "main",
        100,
        api_key=" key-1 ",
        fallback_base_url="http://fb.test",
        fallback_model="fb-model",
        vision_model="vis",
    )
    names = [ep.name for ep in p._endpoints]
    assert names == ["primary", "fallback", "vision"]
    assert p.base_url == "http://primary.test/v1"
    assert p._endpoint_named("fallback").base_url == "http://fb.test/v1"
    vision = p._endpoint_named("vision")
    # Vision inherits primary URL and key when none are given.
    assert vision.base_url == p.base_url and vision.api_key == "key-1"
    assert p._endpoint_named("nope") is None


def test_fallback_requires_both_url_and_model():
    p = OpenAICompatibleProvider("http://p.test", "m", 1, fallback_model="fb")
    assert [ep.name for ep in p._endpoints] == ["primary"]


def test_retry_and_empty_budgets_are_clamped(monkeypatch):
    p = OpenAICompatibleProvider("http://p.test", "m", 1, retry_attempts=0)
    assert p.retry_attempts == 1
    monkeypatch.setenv("AI_EMPTY_RESPONSE_RETRIES", "99")
    assert OpenAICompatibleProvider("http://p.test", "m", 1).empty_response_retries == 5
    monkeypatch.setenv("AI_EMPTY_RESPONSE_RETRIES", "lots")
    p = OpenAICompatibleProvider("http://p.test", "m", 1)
    assert p.empty_response_retries == providers_mod.DEFAULT_EMPTY_RESPONSE_RETRIES


def test_policy_forbidding_fallback_rejects_alternate_routes():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(
            "https://p.test",
            "m",
            1,
            fallback_model="fb",
            policy=ProviderPolicy(allow_provider_fallback=False),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test/v1",
        "https://user:pw@api.example.test/v1",
        "https://10.0.0.5/v1",
        "https://127.0.0.1/v1",
        "https:///v1",
    ],
)
def test_public_only_policy_rejects_unsafe_base_urls(url):
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(
            url, "m", 1, policy=ProviderPolicy(public_network_only=True)
        )


def test_public_only_policy_accepts_https_hostnames_and_public_ips():
    policy = ProviderPolicy(public_network_only=True)
    OpenAICompatibleProvider("https://api.example.test/v1", "m", 1, policy=policy)
    OpenAICompatibleProvider("https://8.8.8.8/v1", "m", 1, policy=policy)


def test_policy_validation():
    with pytest.raises(ValueError):
        ProviderPolicy(max_request_seconds=0)
    with pytest.raises(ValueError):
        ProviderPolicy(public_network_only=True, allow_redirects=True)


def test_headers_bearer_and_opencode_session(monkeypatch):
    p = OpenAICompatibleProvider("https://opencode.ai/zen/v1", "m", 1, api_key="k")
    assert p._headers() == {
        "Authorization": "Bearer k",
        "x-opencode-session": "maxwell",
    }
    monkeypatch.setenv("OPENCODE_SESSION", " sess ")
    assert p._headers()["x-opencode-session"] == "sess"
    plain = OpenAICompatibleProvider("http://p.test", "m", 1)
    assert plain._headers() == {}


def test_auth_header_source_failure_is_an_authentication_error():
    class Broken:
        async def headers(self):
            raise RuntimeError("vault locked")

    p = OpenAICompatibleProvider("http://p.test", "m", 1, authentication=Broken())
    with pytest.raises(ProviderAuthenticationError):
        run(p._auth_headers(p._endpoints[0]))


def test_display_error_redacts_and_byok_hides_bodies():
    p = OpenAICompatibleProvider("http://p.test", "m", 1, api_key="sk-secret")
    assert p._display_error("bad key sk-secret and tok", ("tok",)) == (
        "bad key [redacted] and [redacted]"
    )
    byok = OpenAICompatibleProvider(
        "https://p.test", "m", 1, policy=ProviderPolicy(sensitive_credentials=True)
    )
    assert "sk-" not in byok._display_error("sk-secret")


def test_redact_payload_recurses_only_for_sensitive_policy():
    plain = OpenAICompatibleProvider("http://p.test", "m", 1, api_key="k1")
    value = {"a": ["k1"]}
    assert plain._redact_provider_payload(value) is value
    byok = OpenAICompatibleProvider(
        "https://p.test",
        "m",
        1,
        api_key="k1",
        policy=ProviderPolicy(sensitive_credentials=True),
    )
    out = byok._redact_provider_payload({"k1": ["x k1 y", {"n": "k1"}], "num": 3})
    assert out == {"[redacted]": ["x [redacted] y", {"n": "[redacted]"}], "num": 3}


@pytest.mark.parametrize(
    "limit, expected", [(None, 10 * 1024 * 1024), (5, 5), (10**12, 10 * 1024 * 1024)]
)
def test_response_limit_is_capped(limit, expected):
    p = provider(policy=ProviderPolicy(max_response_bytes=limit))
    assert p._response_limit() == expected


# --------------------------------------------------------------------------
# payload building
# --------------------------------------------------------------------------


def _payload(p, **kw):
    ep = kw.pop("endpoint", p._endpoints[0])
    return p._request_payload(ep, [{"role": "user", "content": "x"}], **kw)


def test_payload_defaults():
    p = provider()
    data = _payload(p)
    assert data["model"] == "main-model"
    assert data["stream"] is True
    assert data["max_tokens"] == 512
    assert data["stream_options"] == {"include_usage": True}
    assert data["reasoning_effort"] == "none"
    assert data["reasoning"] == {"effort": "none"}
    assert data["thinking"] == {"type": "disabled", "budget_tokens": 0}
    assert "tools" not in data


def test_payload_tools_and_model_override_on_primary_only():
    p = OpenAICompatibleProvider(
        "http://p.test",
        "main",
        100,
        fallback_base_url="http://f.test",
        fallback_model="fb",
    )
    tools = [{"type": "function", "function": {"name": "t"}}]
    data = _payload(p, tools=tools, model="override")
    assert data["model"] == "override"
    assert data["tools"] == tools and data["tool_choice"] == "auto"
    fb = _payload(p, endpoint=p._endpoint_named("fallback"), model="override")
    assert fb["model"] == "fb"


def test_payload_reasoning_effort_when_reasoning_enabled():
    p = provider(disable_reasoning=False, reasoning_effort=" low ")
    data = _payload(p)
    assert data["reasoning_effort"] == "low"
    assert "thinking" not in data and "reasoning" not in data
    # Per-call override wins over the endpoint default.
    assert _payload(p, disable_reasoning=True)["reasoning_effort"] == "none"


def test_payload_kimi_pins_thinking_on_when_reasoning_enabled():
    p = OpenAICompatibleProvider(
        "http://p.test", "kimi-k2.7-code", 10, disable_reasoning=False
    )
    assert _payload(p)["thinking"] == {"type": "enabled"}


def test_payload_clamps_to_learned_output_cap_per_endpoint_and_model():
    p = provider()
    p._endpoint_output_caps[("primary", "main-model")] = 100
    assert _payload(p)["max_tokens"] == 100
    assert _payload(p, max_tokens=50)["max_tokens"] == 50
    assert _payload(p, model="other")["max_tokens"] == 512


def test_payload_omits_stream_usage_for_endpoints_that_rejected_it():
    p = provider()
    p._endpoints_without_stream_usage.add("primary")
    assert "stream_options" not in _payload(p)


def test_payload_drops_fields_the_host_rejected(monkeypatch):
    p = provider()
    monkeypatch.setitem(
        providers_mod._REJECTED_BODY_FIELDS,
        (p.base_url, "main-model"),
        ("thinking", "reasoning"),
    )
    data = _payload(p)
    assert "thinking" not in data and "reasoning" not in data
    assert data["reasoning_effort"] == "none"


def test_media_endpoint_order_prefers_vision_and_demotes_incapable():
    p = OpenAICompatibleProvider(
        "http://p.test",
        "m",
        1,
        fallback_base_url="http://f.test",
        fallback_model="f",
        vision_model="v",
    )
    assert [e.name for e in p._media_endpoint_order()] == [
        "vision",
        "primary",
        "fallback",
    ]
    p._media_incapable.add("vision")
    assert [e.name for e in p._media_endpoint_order()][-1] == "vision"


def test_attempt_endpoint_rotation_and_cooldown():
    p = OpenAICompatibleProvider(
        "http://p.test", "m", 1, fallback_base_url="http://f.test", fallback_model="f"
    )
    assert [p._attempt_endpoint(a).name for a in (1, 2, 3)] == [
        "primary",
        "primary",
        "fallback",
    ]
    assert p._attempt_endpoint(2, fast_fallback=True).name == "fallback"
    assert p._attempt_endpoint(1, prefer_fallback=True).name == "fallback"
    p._cool_endpoint("primary")
    assert p._is_endpoint_cooling("primary")
    assert p._attempt_endpoint(1).name == "fallback"
    p._endpoint_cooldown["primary"] = 0  # already expired
    assert not p._is_endpoint_cooling("primary")
    assert "primary" not in p._endpoint_cooldown


def test_strip_media_parts_flattens_and_counts_dropped_parts():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:"}},
                {"type": "input_audio"},
            ],
        },
        {"role": "assistant", "content": "plain"},
    ]
    assert _strip_media_parts(msgs) is True
    assert msgs[0]["content"].startswith("look\n[2 attachment(s) omitted")
    assert msgs[1]["content"] == "plain"
    assert _strip_media_parts([{"role": "user", "content": "x"}]) is False


# --------------------------------------------------------------------------
# request / response handling
# --------------------------------------------------------------------------


def test_successful_completion_posts_to_chat_completions_with_auth():
    session = Session(ok("pong"))
    p = provider(session, api_key="k-test")
    msg = chat(p)
    assert msg["content"] == "pong"
    req = session.requests[0]
    assert req["url"] == "http://primary.test/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer k-test"
    assert req["json"]["stream"] is False
    assert "stream_options" not in req["json"]


def test_caller_messages_are_not_mutated():
    session = Session(ok())
    p = provider(session)
    messages = [{"role": "user", "content": "ping"}]
    run(p.generate_chat_completion(messages, stream=False))
    assert messages == [{"role": "user", "content": "ping"}]
    assert session.requests[0]["json"]["messages"] is not messages


def test_unavailable_provider_reinitializes_then_fails():
    session = Session(Resp(500))
    p = provider(session, available=False)
    with pytest.raises(ProviderUnavailableError, match="not available"):
        chat(p)
    assert session.requests[0]["url"].endswith("/models")


def test_unavailable_provider_recovers_when_models_endpoint_answers():
    session = Session(Resp(200), ok("back"))
    p = provider(session, available=False)
    assert chat(p)["content"] == "back"
    assert p.available is True


def test_503_retries_then_succeeds():
    session = Session(Resp(503, text="busy"), ok("done"))
    assert chat(provider(session))["content"] == "done"
    assert len(session.requests) == 2


def test_503_exhausts_retries_with_unavailable_error():
    session = Session(*[Resp(503, text="busy")] * 3)
    with pytest.raises(ProviderUnavailableError, match="overloaded"):
        chat(provider(session))
    assert len(session.requests) == 3


def test_429_rate_limit_cools_endpoint_and_raises_after_retries():
    session = Session(*[Resp(429, text="rate limit, slow down")] * 3)
    p = provider(session)
    with pytest.raises(ProviderRateLimitError):
        chat(p)
    assert p._is_endpoint_cooling("primary")


def test_429_quota_exhaustion_on_single_endpoint_raises_immediately():
    session = Session(Resp(429, text="insufficient_quota: out of credits"), ok())
    with pytest.raises(ProviderUsageExhaustedError):
        chat(provider(session))
    assert len(session.requests) == 1


def test_429_on_primary_fails_over_to_fallback():
    session = Session(Resp(429, text="too many requests"), ok("from fallback"))
    p = provider(
        session, fallback_base_url="http://fb.test/v1", fallback_model="fb-model"
    )
    assert chat(p)["content"] == "from fallback"
    assert session.requests[1]["url"] == "http://fb.test/v1/chat/completions"
    assert session.requests[1]["json"]["model"] == "fb-model"


def test_error_body_with_api_key_is_redacted_in_exception():
    body = "invalid request for key sk-live-123"
    session = Session(*[Resp(400, text=body)] * 3)
    p = provider(session, api_key="sk-live-123", retry_attempts=1)
    with pytest.raises(Exception) as info:
        chat(p)
    assert "sk-live-123" not in str(info.value)


def test_network_error_is_retried():
    import aiohttp

    session = Session(aiohttp.ClientConnectionError("reset"), ok("again"))
    assert chat(provider(session))["content"] == "again"


def test_generate_response_returns_provider_result():
    session = Session(ok("answer"))
    result = run(
        provider(session).generate_response(
            [{"role": "user", "content": "q"}], stream=False
        )
    )
    assert result.content == "answer"
    assert list(result.tool_calls) == []


def test_generate_response_joins_list_content_parts():
    parts = [
        {"type": "text", "text": "a"},
        "b",
        {"type": "image"},
        {"type": "text", "text": None},
    ]
    session = Session(ok(parts))
    result = run(
        provider(session).generate_response(
            [{"role": "user", "content": "q"}], stream=False
        )
    )
    assert result.content == "ab"


def test_generate_response_empty_reply_raises():
    session = Session(ok(""), ok(""), ok(""))
    with pytest.raises(ProviderEmptyResponseError):
        run(
            provider(session).generate_response(
                [{"role": "user", "content": "q"}], stream=False
            )
        )


def test_generate_response_retries_without_tools_when_rejected(monkeypatch):
    p = provider()
    calls = []

    async def fake_completion(messages, **kwargs):
        calls.append(kwargs.get("tools"))
        if kwargs.get("tools"):
            raise RuntimeError("this model does not support tools")
        return {"content": "plain"}

    monkeypatch.setattr(p, "generate_chat_completion", fake_completion)
    tools = [{"type": "function", "function": {"name": "t"}}]
    result = run(p.generate_response([{"role": "user", "content": "q"}], tools=tools))
    assert result.content == "plain"
    assert calls == [tools, None]


def test_generate_response_does_not_retry_unrelated_errors(monkeypatch):
    p = provider()

    async def fake_completion(messages, **kwargs):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(p, "generate_chat_completion", fake_completion)
    with pytest.raises(RuntimeError, match="exploded"):
        run(p.generate_response([{"role": "user", "content": "q"}], tools=[{"x": 1}]))


def test_policy_deadline_turns_slow_calls_into_unavailable(monkeypatch):
    p = provider(policy=ProviderPolicy(max_request_seconds=1))

    async def slow(*_a, **_k):
        await asyncio.Event().wait()

    monkeypatch.setattr(p, "generate_chat_completion", slow)

    async def go():
        real_timeout = asyncio.timeout

        def fast_timeout(_seconds):
            return real_timeout(0.01)

        monkeypatch.setattr(providers_mod.asyncio, "timeout", fast_timeout)
        return await p.generate_response([{"role": "user", "content": "q"}])

    with pytest.raises(ProviderUnavailableError, match="time limit"):
        run(go())


def test_close_closes_open_session():
    session = Session()
    p = provider(session)
    run(p.close())
    assert session.closed is True


# --------------------------------------------------------------------------
# bounded readers and resolver
# --------------------------------------------------------------------------


class _Chunks:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _n):
        for c in self._chunks:
            yield c

    async def read(self, n):
        if not self._chunks:
            return b""
        head = self._chunks.pop(0)
        if len(head) > n:
            self._chunks.insert(0, head[n:])
            head = head[:n]
        return head


class _Body:
    def __init__(self, chunks, charset=None):
        self.content = _Chunks(chunks)
        self.charset = charset


def test_text_reader_truncates_at_limit():
    out = run(_read_response_text_limited(_Body([b"a" * 10, b"b" * 10]), limit=15))
    assert out == "a" * 10 + "b" * 5


def test_text_reader_falls_back_to_text_and_swallows_errors():
    class NoContent:
        content = None

        async def text(self):
            return "x" * 20

    class Broken:
        content = None

        async def text(self):
            raise RuntimeError

    assert run(_read_response_text_limited(NoContent(), limit=5)) == "xxxxx"
    assert run(_read_response_text_limited(Broken())) == ""


def test_json_reader_parses_enforces_limit_and_rejects_garbage():
    body = json.dumps({"a": 1}).encode()
    assert run(_read_json_response_limited(_Body([body]), 1024)) == {"a": 1}
    with pytest.raises(RuntimeError, match="size limit"):
        run(_read_json_response_limited(_Body([b"x" * 50]), 10))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        run(_read_json_response_limited(_Body([b"{nope"]), 1024))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        run(_read_json_response_limited(_Body([body], charset="no-such-codec"), 1024))


@pytest.mark.parametrize(
    "ip, public",
    [
        ("8.8.8.8", True),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("224.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("64:ff9b::808:808", False),
        ("fec0::1", False),
        ("2001:4860:4860::8888", True),
    ],
)
def test_ip_is_public(ip, public):
    assert _ip_is_public(ipaddress.ip_address(ip)) is public


def test_public_only_resolver_rejects_private_literals_and_lookups(monkeypatch):
    resolver = _PublicOnlyResolver()

    async def go():
        with pytest.raises(OSError):
            await resolver.resolve("10.0.0.1", 443)
        ok_literal = await resolver.resolve("[2001:4860:4860::8888]", 443)
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(host, port, **_k):
            return [(2, 1, 6, "", ("192.168.1.10", port))]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(OSError, match="non-public"):
            await resolver.resolve("evil.test", 443)

        async def public_getaddrinfo(host, port, **_k):
            return [(2, 1, 6, "", ("8.8.4.4", port))]

        monkeypatch.setattr(loop, "getaddrinfo", public_getaddrinfo)
        resolved = await resolver.resolve("good.test", 443)

        async def empty(host, port, **_k):
            return []

        monkeypatch.setattr(loop, "getaddrinfo", empty)
        with pytest.raises(OSError, match="did not resolve"):
            await resolver.resolve("none.test", 443)
        await resolver.close()
        return ok_literal, resolved

    literal, resolved = run(go())
    assert literal[0]["host"] == "2001:4860:4860::8888"
    assert resolved == [
        {
            "hostname": "good.test",
            "host": "8.8.4.4",
            "port": 443,
            "family": 2,
            "proto": 6,
            "flags": __import__("socket").AI_NUMERICHOST,
        }
    ]


# --------------------------------------------------------------------------
# streaming tool-call accumulation
# --------------------------------------------------------------------------


def test_append_arguments_concatenates_strings_and_accepts_objects():
    slot = {}
    _append_tool_call_arguments(slot, '{"a":')
    _append_tool_call_arguments(slot, " 1}")
    assert slot["function"]["arguments"] == '{"a": 1}'
    _append_tool_call_arguments(slot, None)
    assert slot["function"]["arguments"] == '{"a": 1}'
    obj = {}
    _append_tool_call_arguments(obj, {"b": 2})
    assert json.loads(obj["function"]["arguments"]) == {"b": 2}
    _append_tool_call_arguments(obj, 3)
    assert obj["function"]["arguments"].endswith("3")
    pre = {"function": {"arguments": {"c": 1}}}
    _append_tool_call_arguments(pre, "")
    assert json.loads(pre["function"]["arguments"]) == {"c": 1}


def test_keep_provider_fields_copies_signatures_once():
    slot = {"function": {"name": "t"}}
    delta = {
        "index": 0,
        "id": "x",
        "extra_content": {"google": {"thought_signature": "sig"}},
        "_private": 1,
        "empty": "",
        "function": {
            "name": "t",
            "arguments": "{}",
            "thought_signature": "fsig",
            "_p": 1,
        },
    }
    _keep_tool_call_provider_fields(slot, delta)
    assert slot["extra_content"] == {"google": {"thought_signature": "sig"}}
    assert slot["function"]["thought_signature"] == "fsig"
    assert "_private" not in slot and "empty" not in slot and "id" not in slot
    _keep_tool_call_provider_fields(slot, {"extra_content": {"other": 1}})
    assert slot["extra_content"] == {"google": {"thought_signature": "sig"}}
    _keep_tool_call_provider_fields(slot, "not a dict")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "args, expected",
    [
        ("", ""),
        ('{"reasoning": "full"}', "full"),
        ('{"reasoning": "line\\nnext", "x": ', "line\nnext"),
        ('{"reasoning": "still typing', ""),
        ({"reasoning": "dict"}, "dict"),
        ({"reasoning": 5}, ""),
        ('{"other": 1}', ""),
        (12, ""),
    ],
)
def test_extract_partial_reasoning(args, expected):
    assert _extract_partial_reasoning(args) == expected


# --------------------------------------------------------------------------
# error classification
# --------------------------------------------------------------------------


def test_context_output_limit():
    text = "maximum context length is 8000 tokens. However, you requested about 9000 tokens"
    assert rules.context_output_limit(400, text, 2000) == 1000 - 125
    assert rules.context_output_limit(500, text, 2000) is None
    assert rules.context_output_limit(400, "other", 2000) is None
    # Requested less than current output: not recoverable from these counts.
    assert rules.context_output_limit(400, text, 10_000) is None


def test_maximum_output_limit():
    assert rules.maximum_output_limit(400, "maximum output tokens (4096)", 8000) == 4032
    assert rules.maximum_output_limit(400, "maximum output tokens 4096", 1000) is None
    assert rules.maximum_output_limit(429, "maximum output tokens 4096", 8000) is None


@pytest.mark.parametrize(
    "status, text, exhausted",
    [
        (429, "insufficient_quota", True),
        (429, "Rate limit exceeded", False),
        (429, "too many requests; quota exceeded; billing", True),
        (429, "QuotaExhausted for gemini-3-flash", False),
        (429, "QuotaExhausted for gemini-3-flash, insufficient credits", True),
        (429, "resource_exhausted for this provider project", True),
        (500, "insufficient_quota", False),
    ],
)
def test_usage_exhaustion_classification(status, text, exhausted):
    assert rules._is_usage_exhausted_error(status, text) is exhausted


@pytest.mark.parametrize(
    "status, text, unsupported",
    [
        (404, "No endpoints found that support image input", True),
        (400, "unknown variant `image_url`", True),
        (415, "model does not support images", True),
        (404, "This model does not support input audio", True),
        (500, "does not support images", False),
        (400, "bad request", False),
    ],
)
def test_media_unsupported_classification(status, text, unsupported):
    assert rules._is_media_unsupported_error(status, text) is unsupported


def test_policy_and_stream_option_classification():
    assert rules._is_content_policy_block(400, "PROHIBITED_CONTENT")
    assert not rules._is_content_policy_block(500, "PROHIBITED_CONTENT")
    assert rules._is_policy_block_text("The prompt could not be submitted.")
    assert not rules._is_policy_block_text("a normal reply")
    assert rules._is_stream_options_rejected(400, "unknown field stream_options")
    assert not rules._is_stream_options_rejected(500, "stream_options")


def test_forbidden_body_fields_never_drops_required_fields():
    text = json.dumps(
        {
            "detail": [
                {"type": "extra_forbidden", "loc": ["body", "thinking"]},
                {"type": "extra_forbidden", "loc": ["body", "model"]},
                {"type": "extra_forbidden", "loc": ["body", "thinking"]},
                {"type": "extra_forbidden", "loc": ["body", "reasoning"]},
            ]
        }
    )
    assert rules.forbidden_body_fields(422, text) == ("thinking", "reasoning")
    assert rules.forbidden_body_fields(500, text) == ()
    assert rules.forbidden_body_fields(422, '"loc": ["body", "x"]') == ()
