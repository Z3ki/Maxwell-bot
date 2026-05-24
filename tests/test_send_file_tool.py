import asyncio
import base64

from bot_tools import SendFileTool
from bot_tools import ShellTool
from bot_tools import ReasoningLogTool


class FakeMessage:
    def __init__(self):
        self.id = 99
        self.content = "what should we do?"
        self.created_at = "2026-05-24T11:00:00Z"
        self.files = []
        self.replies = []
        class FakeChannel:
            def __init__(self, outer):
                self.outer = outer
                self.id = 123
                self.name = "general"
            async def send(self, content=None, file=None, **kwargs):
                self.outer.replies.append(content)
                if file is not None:
                    self.outer.files.append(file)
        self.channel = FakeChannel(self)
        class FakeAuthor:
            id = "1325265045600600135"  # Mock admin ID to bypass authorization gate
            display_name = "alice"
            bot = False
        self.author = FakeAuthor()
        self.guild = type("FakeGuild", (), {"id": 456, "name": "guild"})()
        self.mentions = []
        self.attachments = []
        self.embeds = []
        self.reference = None

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

    payload = bot.traces[0]
    assert payload["model_supplied"] == {
        "intent": "reply",
        "confidence": 0.82,
        "thoughts": "Need answer directly.",
        "data": {"raw": [1, 2, 3]},
    }
    assert payload["runtime_context"]["message_content"] == "what should we do?"
    assert payload["runtime_context"]["channel_name"] == "general"
    assert payload["runtime_context"]["guild_name"] == "guild"
    assert payload["runtime_context"]["author_name"] == "alice"
