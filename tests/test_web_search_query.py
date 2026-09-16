import asyncio
from types import SimpleNamespace

from bot import TOOL_PROTOCOL, MaxwellBot
from bot_tools import (
    FetchUrlTool,
    WebSearchTool,
    _format_web_hits,
    _normalize_web_hit,
    _sanitize_web_query,
    _web_search_backends,
)
from tool_schemas import RESULT_TOOL_NAMES, build_openai_tools


GLUED = (
    "How much usage would I get with this on ollama cloud 20$ plan\n"
    "[Latest message replies to you/Maxwell(1382894657624866889): they don't "
    "publish a number. **ol"
)


def test_no_forced_auto_web_search_helpers():
    assert not hasattr(MaxwellBot, "_needs_up_to_date_info")
    assert not hasattr(MaxwellBot, "_extract_search_query")


def test_sanitize_web_query_truncates_unclosed_bracket():
    q = _sanitize_web_query(GLUED)
    assert "Latest message" not in q
    assert q.startswith("How much usage")


def test_web_search_description_encourages_lookup():
    desc = WebSearchTool(SimpleNamespace()).get_description().lower()
    assert "don't search" not in desc
    assert "only if" not in desc
    assert "casual conversation" not in desc
    assert "unsure" in desc or "guess" in desc
    assert "automatically" in desc
    stamped = build_openai_tools({"web_search": WebSearchTool(SimpleNamespace())})[0][
        "function"
    ]["description"].lower()
    assert "returns output" in stamped
    assert "don't search" not in stamped


def test_fetch_url_description_is_for_reading_pages():
    desc = FetchUrlTool(SimpleNamespace()).get_description().lower()
    assert "only if" not in desc
    assert "page" in desc
    assert "web_search" in desc


def test_tool_protocol_says_look_things_up():
    blob = TOOL_PROTOCOL.lower()
    assert "web_search" in blob
    assert "fetch_url" in blob
    assert "guess" in blob
    assert "training data" in blob
    assert "nothing is searched for you" in blob


def test_native_tool_prompt_includes_lookup_contract():
    bot = SimpleNamespace(
        tools={
            "web_search": WebSearchTool(SimpleNamespace()),
            "fetch_url": FetchUrlTool(SimpleNamespace()),
            "send_message": SimpleNamespace(get_description=lambda: "send"),
        },
        _control={
            "tools_enabled": True,
            "disabled_tools": [],
            "native_tool_calls": True,
        },
    )
    bot._compatible_tool_names = MaxwellBot._compatible_tool_names.__get__(bot)
    prompt = MaxwellBot._tool_system_prompt(bot, "discord")
    assert "XML text tags only" not in prompt
    assert "nothing is looked up automatically" in prompt.lower()
    assert "web_search" in prompt
    assert "training data" in prompt
    assert TOOL_PROTOCOL in prompt


def test_lookup_tools_return_output_to_the_model():
    assert "web_search" in RESULT_TOOL_NAMES
    assert "fetch_url" in RESULT_TOOL_NAMES


def test_normalize_web_hit_accepts_url_and_excerpt():
    hit = _normalize_web_hit(
        {"title": "T", "url": "https://ex.com/a", "excerpt": "hello body"}
    )
    assert hit["href"] == "https://ex.com/a"
    assert hit["body"] == "hello body"
    formatted = _format_web_hits([hit])
    assert "https://ex.com/a" in formatted
    assert "hello body" in formatted


def _search_bot():
    return SimpleNamespace(
        mark_message_tainted=lambda *_a, **_k: None,
        config=SimpleNamespace(RAG_WEB_STORE_ENABLED=False),
        memory=None,
    )


def test_web_search_formats_url_keyed_hits(monkeypatch):
    class FakeDDGS:
        def __init__(self, *a, **k):
            pass

        def text(self, query, **k):
            return [
                {
                    "title": "Example",
                    "url": "https://ex.com/page",
                    "excerpt": "A" * 80,
                }
            ]

    monkeypatch.setattr("bot_tools._DDGS", FakeDDGS)
    monkeypatch.setattr("bot_tools._DDGS_AVAILABLE", True)
    tool = WebSearchTool(_search_bot())
    result = asyncio.run(tool.execute(SimpleNamespace(guild=None), query="mat dickie"))
    assert not result.lower().startswith("error")
    assert "https://ex.com/page" in result
    assert "Example" in result
    assert "A" * 80 in result


def test_web_search_empty_ddgs_exception_is_not_an_error(monkeypatch):
    class FakeDDGS:
        def __init__(self, *a, **k):
            pass

        def text(self, query, **k):
            raise RuntimeError("No results found.")

    monkeypatch.setattr("bot_tools._DDGS", FakeDDGS)
    monkeypatch.setattr("bot_tools._DDGS_AVAILABLE", True)
    tool = WebSearchTool(_search_bot())
    result = asyncio.run(tool.execute(SimpleNamespace(guild=None), query="xyzzy"))
    assert result.startswith("No results found")
    assert not result.lower().startswith("error")


def test_web_search_default_backends_skip_brave_google():
    backends = _web_search_backends(None)
    assert backends[0] == "duckduckgo"
    assert "brave" not in backends
    assert "google" not in backends
    hinted = _web_search_backends("brave")
    assert hinted[0] == "brave"
    assert "duckduckgo" in hinted


def test_web_search_falls_back_when_primary_backend_fails(monkeypatch):
    calls = []

    class FakeDDGS:
        def __init__(self, *a, **k):
            pass

        def text(self, query, **k):
            backend = k.get("backend")
            calls.append(backend)
            if backend == "duckduckgo":
                raise RuntimeError("429 Too Many Requests")
            if backend == "bing":
                return [{"title": "B", "href": "https://ex.com/b", "body": "ok"}]
            return []

    monkeypatch.setattr("bot_tools._DDGS", FakeDDGS)
    monkeypatch.setattr("bot_tools._DDGS_AVAILABLE", True)
    tool = WebSearchTool(_search_bot())
    result = asyncio.run(tool.execute(SimpleNamespace(guild=None), query="mat dickie"))
    assert not result.lower().startswith("error")
    assert "https://ex.com/b" in result
    assert calls[0] == "duckduckgo"
    assert "bing" in calls


def test_web_search_taints_the_turn(monkeypatch):
    tainted = {}

    class FakeDDGS:
        def __init__(self, *a, **k):
            pass

        def text(self, query, **k):
            return [{"title": "T", "href": "https://ex.com", "body": "b"}]

    monkeypatch.setattr("bot_tools._DDGS", FakeDDGS)
    monkeypatch.setattr("bot_tools._DDGS_AVAILABLE", True)
    bot = _search_bot()
    bot.mark_message_tainted = lambda msg: tainted.setdefault("ok", True)
    msg = SimpleNamespace(id=9, guild=None)
    asyncio.run(WebSearchTool(bot).execute(msg, query="hi"))
    assert tainted.get("ok") is True
