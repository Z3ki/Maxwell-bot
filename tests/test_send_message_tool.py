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


def test_send_message_memory_fallback_fetches_once():
    class MemChannel:
        def __init__(self):
            self.id = 99
            self.fetched = []
            self.by_id = {}

        async def history(self, limit=40):
            if False:
                yield None

        async def fetch_message(self, mid):
            self.fetched.append(int(mid))
            return self.by_id[int(mid)]

    class Memory:
        async def get_channel_memory(self, _cid):
            rows = [
                {"message_id": str(i), "author": "spam", "content": f"noise {i}"}
                for i in range(200, 400)
            ]
            rows.append({"message_id": "2", "author": "Alice", "content": "nah"})
            return rows

    async def run():
        channel = MemChannel()
        latest = _HistMessage(3, "what?", "Z3ki", channel)
        nah = _HistMessage(2, "nah", "Alice", channel)
        channel.by_id = {2: nah, 3: latest}
        bot = SimpleNamespace(memory=Memory())
        target = await resolve_send_reply_target(
            latest, reply_to="nah", bot=bot
        )
        assert target is nah
        assert channel.fetched == [2]

    asyncio.run(run())


def test_send_message_uses_slowmode_helper_when_present():
    calls = []

    async def fake_send(channel, content=None, reply_to=None, **_kw):
        calls.append((content, reply_to))
        if reply_to is not None:
            await reply_to.reply(content)
        else:
            await channel.send(content)
        return SimpleNamespace(id=1)

    async def run():
        tool = SendMessageTool(SimpleNamespace(_send_with_slowmode=fake_send))
        message = FakeMessage()
        await tool.execute(message, content="hi")
        assert calls == [("hi", message)]
        assert message.replies == ["hi"]

    asyncio.run(run())


def test_send_message_explicit_channel_id():
    class FakeTargetChannel:
        def __init__(self, cid):
            self.id = cid
            self.sent = []

        async def send(self, text):
            self.sent.append(text)

    class FakeBot:
        def __init__(self):
            self.channels = {123456789: FakeTargetChannel(123456789)}

        def get_channel(self, cid):
            return self.channels.get(cid)

        async def fetch_channel(self, cid):
            if cid in self.channels:
                return self.channels[cid]
            raise RuntimeError("Channel not found")

    async def run():
        bot = FakeBot()
        tool = SendMessageTool(bot)
        origin_msg = FakeMessage()

        # Test string channel_id
        res = await tool.execute(origin_msg, content="hello target channel", channel_id="123456789")
        assert "__MESSAGE_SENT__" in res
        assert bot.channels[123456789].sent == ["hello target channel"]
        assert origin_msg.channel.sent == []
        assert origin_msg.replies == []

        # Test int channel_id
        res2 = await tool.execute(origin_msg, content="hello again", channel_id=123456789)
        assert "__MESSAGE_SENT__" in res2
        assert bot.channels[123456789].sent == ["hello target channel", "hello again"]

        # Test invalid channel_id
        res3 = await tool.execute(origin_msg, content="nope", channel_id="invalid_id")
        assert "__MESSAGE_SENT__" in res3
        # When channel not found, fallback sends to origin_msg channel
        assert origin_msg.replies == ["nope"]

        # Test replying when target message is a SimpleNamespace without .reply
        from types import SimpleNamespace
        sn_msg = SimpleNamespace(
            channel=SimpleNamespace(id="chan1", send=origin_msg.channel.send),
            id="msg123",
            content="klipy link https://klipy.co/123",
        )
        res4 = await tool.execute(sn_msg, content="nice link")
        assert "__MESSAGE_SENT__" in res4

    asyncio.run(run())

