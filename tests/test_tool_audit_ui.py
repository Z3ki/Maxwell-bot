import asyncio
from pathlib import Path
from types import SimpleNamespace

from plugins.maxwell_extras.audit_ui import install_tool_audit


class _Ctx:
    def __init__(self, data):
        self.data = Path(data)

    def store_path(self, name):
        return self.data / name


class _Sent:
    def __init__(self, mid):
        self.id = mid
        self.view = None

    async def edit(self, **kwargs):
        if "view" in kwargs:
            self.view = kwargs["view"]
        return self


class _Web:
    async def execute(self, message, query=None, **kwargs):
        return f"results for {query}"


def test_audit_records_dispatcher_and_direct_web_search_and_attaches_button(tmp_path):
    bot = SimpleNamespace()
    bot.tools = {"web_search": _Web()}

    async def execute_tool(message, name, params, *, disabled, compatible):
        if name == "web_search":
            return await bot.tools["web_search"].execute(message, query=params.get("query"))
        return "ok"

    async def send(channel, content=None, *, reply_to=None, file=None, **kwargs):
        return _Sent(900)

    bot._execute_tool_by_name = execute_tool
    bot._send_with_slowmode = send
    install_tool_audit(bot, _Ctx(tmp_path))
    message = SimpleNamespace(id=100, channel=SimpleNamespace(id=5))

    async def run():
        await bot._execute_tool_by_name(
            message,
            "web_search",
            {"query": "manual query"},
            disabled=set(),
            compatible={"web_search"},
        )
        await bot.tools["web_search"].execute(message, query="direct query")
        sent = await bot._send_with_slowmode(
            message.channel, "answer", reply_to=message
        )
        row = await bot._maxwell_tool_audit_store.get_response("900")
        assert row is not None
        calls = row["calls"]
        assert [c["name"] for c in calls] == ["web_search", "web_search"]
        assert [c["source"] for c in calls] == ["model", "direct"]
        assert sent.view is not None
        assert sent.view.children[0].label == "Tools · 2"

    asyncio.run(run())


def test_audit_skips_send_message_and_does_not_attach_button(tmp_path):
    bot = SimpleNamespace(tools={})

    async def execute_tool(message, name, params, *, disabled, compatible):
        return "__MESSAGE_SENT__\nhey z3ki! what's up?"

    async def send(channel, content=None, *, reply_to=None, file=None, **kwargs):
        return _Sent(902)

    bot._execute_tool_by_name = execute_tool
    bot._send_with_slowmode = send
    install_tool_audit(bot, _Ctx(tmp_path))
    message = SimpleNamespace(id=102, channel=SimpleNamespace(id=5))

    async def run():
        await bot._execute_tool_by_name(
            message,
            "send_message",
            {"content": "hey z3ki! what's up?"},
            disabled=set(),
            compatible={"send_message"},
        )
        sent = await bot._send_with_slowmode(
            message.channel, "hey z3ki! what's up?", reply_to=message
        )
        assert await bot._maxwell_tool_audit_store.get_response("902") is None
        assert sent.view is None
        assert not await bot._maxwell_tool_audit_store.has_live("102")

    asyncio.run(run())


def test_audit_hook_path_skips_delivery_and_times_hidden_tools(tmp_path):
    hooks: list[tuple[str, object]] = []

    class _HookCtx(_Ctx):
        def register_hook(self, hook, callback, *, priority=100):
            del priority
            hooks.append((hook, callback))

    bot = SimpleNamespace(tools={})

    async def send(channel, content=None, *, reply_to=None, file=None, **kwargs):
        return _Sent(903)

    bot._send_with_slowmode = send
    install_tool_audit(bot, _HookCtx(tmp_path))
    registered = dict(hooks)
    assert "before_tool" in registered
    assert "after_tool" in registered
    message = SimpleNamespace(id=103, channel=SimpleNamespace(id=5))

    async def run():
        await registered["before_tool"](
            {
                "bot": bot,
                "message": message,
                "name": "send_message",
                "params": {"content": "hi"},
                "tool": SimpleNamespace(produces_visible_output=True),
            }
        )
        await registered["after_tool"](
            {
                "bot": bot,
                "message": message,
                "name": "send_message",
                "params": {"content": "hi"},
                "tool": SimpleNamespace(produces_visible_output=True),
                "result": "__MESSAGE_SENT__\nhi",
            }
        )
        await registered["before_tool"](
            {
                "bot": bot,
                "message": message,
                "name": "web_search",
                "params": {"query": "x"},
            }
        )
        await asyncio.sleep(0.01)
        await registered["after_tool"](
            {
                "bot": bot,
                "message": message,
                "name": "web_search",
                "params": {"query": "x"},
                "result": "hits",
            }
        )
        sent = await bot._send_with_slowmode(message.channel, "answer", reply_to=message)
        row = await bot._maxwell_tool_audit_store.get_response("903")
        assert [c["name"] for c in row["calls"]] == ["web_search"]
        assert row["calls"][0]["elapsed_ms"] >= 0
        assert sent.view is not None
        assert sent.view.children[0].label == "Tools · 1"

    asyncio.run(run())


def test_audit_records_non_web_dispatch(tmp_path):
    bot = SimpleNamespace(tools={})

    async def execute_tool(message, name, params, *, disabled, compatible):
        return "Tool shell: done"

    async def send(channel, content=None, *, reply_to=None, file=None, **kwargs):
        return _Sent(901)

    bot._execute_tool_by_name = execute_tool
    bot._send_with_slowmode = send
    install_tool_audit(bot, _Ctx(tmp_path))
    message = SimpleNamespace(id=101, channel=SimpleNamespace(id=5))

    async def run():
        await bot._execute_tool_by_name(
            message,
            "shell",
            {"command": "echo ok"},
            disabled=set(),
            compatible={"shell"},
        )
        await bot._send_with_slowmode(message.channel, "answer", reply_to=message)
        row = await bot._maxwell_tool_audit_store.get_response("901")
        assert row["calls"][0]["name"] == "shell"
        assert row["calls"][0]["source"] == "model"

    asyncio.run(run())
