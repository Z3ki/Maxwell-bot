import asyncio
from types import SimpleNamespace

from bot_tools import (
    SendMessageTool,
    resolve_send_reply_target,
    score_reply_candidate,
)


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


def test_send_message_partial_failure_keeps_sent_marker():
    class FlakyChannel:
        def __init__(self):
            self.sent = []

        async def send(self, text):
            if self.sent:
                raise RuntimeError("second chunk failed")
            self.sent.append(text)

    class FlakyMessage:
        def __init__(self):
            self.channel = FlakyChannel()
            self.replies = []

        async def reply(self, text):
            self.replies.append(text)

    async def run():
        tool = SendMessageTool(SimpleNamespace())
        message = FlakyMessage()
        result = await tool.execute(message, content="x" * 4100)
        assert "__MESSAGE_SENT__" in result
        assert len(message.replies) == 1

    asyncio.run(run())


def test_score_reply_candidate_uses_quote_or_name():
    assert score_reply_candidate("nah", content="nah") == 100
    assert score_reply_candidate("nah", content="banana") == 0
    assert score_reply_candidate("alice", author="Alice") >= 75
    assert score_reply_candidate("what?", content="what?") == 100


class _HistChannel:
    def __init__(self, messages):
        self._messages = messages
        self.sent = []

    async def send(self, text):
        self.sent.append(text)

    async def history(self, limit=40):
        for msg in self._messages[:limit]:
            yield msg


class _HistMessage:
    def __init__(self, mid, content, name, channel=None):
        self.id = mid
        self.content = content
        self.author = SimpleNamespace(display_name=name, name=name, id=mid)
        self.channel = channel
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


def test_send_message_reply_to_quote_not_id():
    async def run():
        channel = _HistChannel([])
        latest = _HistMessage(3, "what?", "Z3ki", channel)
        nah = _HistMessage(2, "nah", "Alice", channel)
        lol = _HistMessage(1, "lol", "Bob", channel)
        channel._messages = [latest, nah, lol]
        latest.channel = channel
        tool = SendMessageTool(SimpleNamespace())

        await tool.execute(latest, content="same", reply_to="nah")

        assert nah.replies == ["same"]
        assert latest.replies == []

        target = await resolve_send_reply_target(latest, reply_to="alice")
        assert target is nah
        prev = await resolve_send_reply_target(latest, reply_to="previous")
        assert prev is nah

    asyncio.run(run())
