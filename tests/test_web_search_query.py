import asyncio
from types import SimpleNamespace

import bot as bot_module
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


def test_freshness_classifier_searches_current_facts_not_banter():
    assert MaxwellBot._needs_up_to_date_info("What's the latest Python version?")
    assert MaxwellBot._needs_up_to_date_info(
        "How much usage does the Ollama Cloud 20$ plan include?"
    )
    assert MaxwellBot._needs_up_to_date_info("What changed recently?")
    assert not MaxwellBot._needs_up_to_date_info("Tell me a joke right now.")
    assert not MaxwellBot._needs_up_to_date_info(
        "What do you think of pizza right now?"
    )


def test_search_query_uses_slash_request_and_respects_web_option():
    wrapped = (
        "Auto web mode: current/latest requests use web_search.\n\n"
        "User request:\nWhat's the latest Python version?"
    )
    assert MaxwellBot._extract_search_query(wrapped) == (
        "What's the latest Python version?"
    )
    assert MaxwellBot._automatic_web_search_query(
        SimpleNamespace(user_install_web_mode="off"), wrapped
    ) == ""
    assert MaxwellBot._automatic_web_search_query(
        SimpleNamespace(user_install_web_mode="search"), "Explain photosynthesis"
    ) == "Explain photosynthesis"
    assert MaxwellBot._automatic_web_search_query(
        SimpleNamespace(user_install_web_mode="auto", user_install_mode="research"),
        "Explain photosynthesis",
    ) == "Explain photosynthesis"


def test_sanitize_web_query_truncates_unclosed_bracket():
    q = _sanitize_web_query(GLUED)
    assert "Latest message" not in q
    assert q.startswith("How much usage")


def test_web_off_hides_lookup_tools_from_the_turn_catalog():
    bot = SimpleNamespace(
        _control={"disabled_tools": []},
        tools={
            "web_search": object(),
            "fetch_url": object(),
            "send_message": object(),
        },
        plugin_manager=None,
    )
    message = SimpleNamespace(
        user_install_web_mode="off",
        author=SimpleNamespace(id=7),
        channel=None,
        guild=None,
    )
    hidden = MaxwellBot._turn_tool_names(bot, "discord", message)
    assert {"web_search", "fetch_url"}.isdisjoint(hidden)

    message.user_install_web_mode = "auto"
    visible = MaxwellBot._turn_tool_names(bot, "discord", message)
    assert {"web_search", "fetch_url"} <= visible


def test_web_off_rejects_direct_lookup_tool_calls(monkeypatch):
    calls = []

    class Tool:
        is_destructive = False

        async def execute(self, message, **_params):
            calls.append(message)
            return "unexpected lookup"

    class Breaker:
        @staticmethod
        def is_open(_name):
            return False

        @staticmethod
        def record_failure(_name):
            pass

        @staticmethod
        def record_success(_name):
            pass

    async def record_reasoning(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot_module, "record_reasoning", record_reasoning)
    tool = Tool()
    bot = SimpleNamespace(
        tools={"web_search": tool},
        plugin_manager=None,
        _tool_breaker=Breaker(),
        hooks=None,
    )
    message = SimpleNamespace(user_install_web_mode="off")

    async def run():
        return await MaxwellBot._execute_tool_by_name(
            bot,
            message,
            "web_search",
            {"query": "current fact"},
            disabled=set(),
            compatible={"web_search"},
        )

    result = asyncio.run(run())
    assert "web access is disabled" in result
    assert calls == []




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


def test_tool_protocol_requires_live_sources_for_current_facts():
    blob = TOOL_PROTOCOL.lower()
    assert "web_search" in blob
    assert "fetch_url" in blob
    assert "cite source urls" in blob
    assert "training data" in blob
    assert "untrusted data" in blob
    assert "current/latest requests" in blob


def test_native_tool_prompt_includes_live_lookup_contract():
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
    assert "current/latest requests" in prompt.lower()
    assert "web_search" in prompt
    assert "training data" in prompt
    assert "untrusted data" in prompt
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


def test_preflight_search_executes_before_generation_and_injects_tool_result():
    calls = []
    remembered = []

    async def execute(message, name, params, *, disabled, compatible):
        calls.append((name, params, disabled, compatible))
        return "Tool web_search: 1. Python releases\\nhttps://python.org/downloads/\\nPython 3.x"

    async def remember(message, name, params, result):
        remembered.append((name, params, result))

    bot = SimpleNamespace(
        _control={"tools_enabled": True, "disabled_tools": []},
        _message_tool_platform=lambda _message: "discord",
        _compatible_tool_names=lambda _platform: {"web_search"},
        _execute_tool_by_name=execute,
        _remember_tool_call=remember,
    )
    message = SimpleNamespace(id=42, user_install_web_mode="auto")
    schemas = [{"type": "function", "function": {"name": "web_search"}}]
    messages = [{"role": "user", "content": "What's the latest Python version?"}]

    async def run():
        await MaxwellBot._run_preflight_web_search(
            bot,
            message,
            messages[-1]["content"],
            messages,
            openai_tools=schemas,
            provider_tools=schemas,
            custom_tool_calls=False,
        )

    asyncio.run(run())
    assert calls[0][0] == "web_search"
    assert calls[0][1]["query"] == "What's the latest Python version?"
    assert calls[0][2] == set()
    assert calls[0][3] == {"web_search"}
    assert remembered[0][0] == "web_search"
    assert "untrusted evidence" in messages[1]["content"]
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["tool_calls"][0]["function"]["name"] == "web_search"
    assert messages[-1]["role"] == "tool"
    assert "https://python.org/downloads/" in messages[-1]["content"]


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
