import asyncio
import base64

from bot_tools import SendFileTool
from bot_tools import ShellTool
from bot_tools import ReasoningLogTool


class FakeMessage:
    def __init__(self):
        self.files = []
        self.replies = []
        class FakeChannel:
            def __init__(self, outer):
                self.outer = outer
            async def send(self, content=None, file=None, **kwargs):
                self.outer.replies.append(content)
                if file is not None:
                    self.outer.files.append(file)
        self.channel = FakeChannel(self)
        class FakeAuthor:
            id = "1325265045600600135"  # Mock admin ID to bypass authorization gate
        self.author = FakeAuthor()

    async def send(self, content=None, file=None, **kwargs):
        self.replies.append(content)
        if file is not None:
            self.files.append(file)

    async def reply(self, content=None, file=None, **kwargs):
        self.replies.append(content)
        if file is not None:
            self.files.append(file)


def test_send_file_tool_sends_text_file():
    tool = SendFileTool(bot=None)
    message = FakeMessage()

    async def run():
        result = await tool.execute(message, filename="hello.py", content="print('hi')\n")
        assert result == "__FILE_SENT__ Sent file: hello.py (12 bytes)"
        assert len(message.files) == 1
        sent = message.files[0]
        assert sent.filename == "hello.py"
        sent.fp.seek(0)
        assert sent.fp.read() == b"print('hi')\n"

    asyncio.run(run())


def test_send_file_tool_sends_base64_and_strips_path():
    tool = SendFileTool(bot=None)
    message = FakeMessage()
    payload = base64.b64encode(b"\x00\x01binary").decode("ascii")

    async def run():
        result = await tool.execute(message, filename="../data.bin", content=payload, encoding="base64")
        assert result == "__FILE_SENT__ Sent file: data.bin (8 bytes)"
        sent = message.files[0]
        assert sent.filename == "data.bin"
        sent.fp.seek(0)
        assert sent.fp.read() == b"\x00\x01binary"

    asyncio.run(run())


def test_shell_tool_runs_without_author_gate():
    class FakeBot:
        def _is_admin(self, user_id):
            return True
    tool = ShellTool(bot=FakeBot())
    message = FakeMessage()

    async def run():
        # Set up a fake container runner in ShellTool to bypass docker exec in unit tests
        async def fake_run_shell(command):
            return b"hi", b"", 0
        tool._run_shell_command = fake_run_shell

        result = await tool.execute(message, command="printf hi")
        assert result == "__SHELL_SENT__\n$ printf hi\nhi"
        assert len(message.files) == 0

    asyncio.run(run())


def test_reasoning_log_tool_records_verbose_payload():
    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    bot = FakeBot()
    tool = ReasoningLogTool(bot=bot)
    message = FakeMessage()

    async def run():
        result = await tool.execute(
            message,
            intent="reply",
            confidence=0.82,
            thoughts="Need answer directly.",
            data={"raw": [1, 2, 3]},
        )
        assert result == "__REASONING_RECORDED__"

    asyncio.run(run())

    assert bot.traces == [{
        "thoughts": "Need answer directly.",
        "intent": "reply",
        "confidence": 0.82,
        "data": {"raw": [1, 2, 3]},
    }]
    assert list(bot.traces[0])[:1] == ["thoughts"]


def test_reasoning_log_strips_nested_xml_from_thoughts():
    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    bot = FakeBot()
    tool = ReasoningLogTool(bot=bot)
    message = FakeMessage()

    async def run():
        result = await tool.execute(
            message,
            thoughts="<thoughts>User wants a site</thoughts><intent>create</intent><decision>build</decision> and some extra text",
        )
        assert result == "__REASONING_RECORDED__"

    asyncio.run(run())

    trace = bot.traces[0]
    assert "<thoughts>" not in trace["thoughts"]
    assert "<intent>" not in trace["thoughts"]
    assert "some extra text" in trace["thoughts"]
    assert trace["intent"] == "create"
    assert trace["decision"] == "build"


def test_reasoning_log_preserves_valid_compact_payload():
    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    bot = FakeBot()
    tool = ReasoningLogTool(bot=bot)
    message = FakeMessage()

    async def run():
        result = await tool.execute(
            message,
            thoughts="User asked for a site, so I should create one.",
            intent="create_site",
            decision="use_create_site",
            confidence="high",
        )
        assert result == "__REASONING_RECORDED__"

    asyncio.run(run())

    trace = bot.traces[0]
    assert trace["thoughts"] == "User asked for a site, so I should create one."
    assert trace["intent"] == "create_site"
    assert trace["decision"] == "use_create_site"
    assert trace["confidence"] == "high"


def test_reasoning_log_clamps_long_fields():
    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    bot = FakeBot()
    tool = ReasoningLogTool(bot=bot)
    message = FakeMessage()

    async def run():
        result = await tool.execute(
            message,
            thoughts="x" * 1000,
            intent="y" * 600,
        )
        assert result == "__REASONING_RECORDED__"

    asyncio.run(run())

    trace = bot.traces[0]
    assert len(trace["thoughts"]) <= 500
    assert len(trace["intent"]) <= 500
