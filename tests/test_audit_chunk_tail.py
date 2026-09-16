import asyncio
from pathlib import Path
from types import SimpleNamespace

from plugins.maxwell_extras.audit_chunk_tail import install_audit_chunk_tail
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


def test_tool_disclosure_moves_to_last_split_chunk(tmp_path):
    bot = SimpleNamespace(tools={})
    sent_messages = []

    async def execute_tool(message, name, params, *, disabled, compatible):
        return "Tool shell: done"

    async def send(channel, content=None, *, reply_to=None, file=None, **kwargs):
        sent = _Sent(900 + len(sent_messages))
        sent_messages.append(sent)
        return sent

    bot._execute_tool_by_name = execute_tool
    bot._send_with_slowmode = send
    install_tool_audit(bot, _Ctx(tmp_path))
    assert install_audit_chunk_tail(bot)
    message = SimpleNamespace(id=100, channel=SimpleNamespace(id=5))

    async def worker():
        await bot._execute_tool_by_name(
            message,
            "shell",
            {"command": "echo ok"},
            disabled=set(),
            compatible={"shell"},
        )
        first = await bot._send_with_slowmode(
            message.channel, "chunk one", reply_to=message
        )
        second = await bot._send_with_slowmode(message.channel, "chunk two")
        third = await bot._send_with_slowmode(message.channel, "chunk three")
        # Nothing should be attached while the response task is still producing
        # chunks. The task-completion callback decides which one is actually last.
        assert first.view is None
        assert second.view is None
        assert third.view is None
        return first, second, third

    async def run():
        first, second, third = await asyncio.create_task(worker())
        # Let the task-done callback run the asynchronous final attachment.
        for _ in range(3):
            await asyncio.sleep(0)

        assert first.view is None
        assert second.view is None
        assert third.view is not None
        assert third.view.children[0].label == "Tools · 1"

        assert await bot._maxwell_tool_audit_store.get_response("900") is None
        assert await bot._maxwell_tool_audit_store.get_response("901") is None
        row = await bot._maxwell_tool_audit_store.get_response("902")
        assert row is not None
        assert row["calls"][0]["name"] == "shell"

    asyncio.run(run())


def test_chunk_tail_does_not_change_non_opted_in_audit_bot(tmp_path):
    """Global audit patch keeps legacy immediate behavior for other bot objects."""

    bot = SimpleNamespace(tools={})

    async def execute_tool(message, name, params, *, disabled, compatible):
        return "ok"

    sent = _Sent(777)

    async def send(channel, content=None, *, reply_to=None, file=None, **kwargs):
        return sent

    bot._execute_tool_by_name = execute_tool
    bot._send_with_slowmode = send
    install_tool_audit(bot, _Ctx(tmp_path))
    message = SimpleNamespace(id=55, channel=SimpleNamespace(id=6))

    async def run():
        await bot._execute_tool_by_name(
            message,
            "shell",
            {},
            disabled=set(),
            compatible={"shell"},
        )
        await bot._send_with_slowmode(message.channel, "answer", reply_to=message)
        assert sent.view is not None

    asyncio.run(run())
