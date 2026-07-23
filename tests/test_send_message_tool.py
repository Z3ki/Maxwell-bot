import asyncio
from types import SimpleNamespace

from bot_tools import SendMessageTool


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeMessage:
    def __init__(self):
        self.channel = FakeChannel()
        self.replies = []

    async def reply(self, text):
        self.replies.append(text)


def test_send_message_tool_splits_long_replies():
    async def run():
        tool = SendMessageTool(SimpleNamespace())
        message = FakeMessage()
        text = "x" * 4100

        result = await tool.execute(message, content=text)

        assert "__MESSAGE_SENT__" in result
        assert "3 chunk" not in result
        assert text in result
        assert len(message.replies) == 1
        assert len(message.channel.sent) == 2
        assert all(len(chunk) <= 1900 for chunk in message.replies + message.channel.sent)

    asyncio.run(run())


def test_send_message_tool_non_reply_sends_all_chunks_to_channel():
    async def run():
        tool = SendMessageTool(SimpleNamespace())
        message = FakeMessage()

        await tool.execute(message, content="y" * 2001, reply=False)

        assert message.replies == []
        assert len(message.channel.sent) == 2

    asyncio.run(run())
