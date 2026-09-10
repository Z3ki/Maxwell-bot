import asyncio
from types import SimpleNamespace

from bot_tools import (
    SendMessageTool,
    resolve_send_reply_target,
    score_reply_candidate,
)


class FakeChannel:
    def __init__(self, cid=99, guild=None):
        self.id = cid
        self.sent = []
        self.guild = guild

    async def send(self, text):
        self.sent.append(text)


class FakeMessage:
    def __init__(self, author_id=11, channel_id=99, *, dm=False):
        guild = None if dm else SimpleNamespace(id=1)
        self.channel = FakeChannel(channel_id, guild=guild)
        self.author = SimpleNamespace(id=author_id)
        self.guild = guild
        self.replies = []

    async def reply(self, text):
        self.replies.append(text)


def test_send_message_appends_ttft_tps_suffix():
    async def run():
        bot = SimpleNamespace(
            ai_provider=SimpleNamespace(
                _last_timing={"ttft_ms": 200.0, "tps": 50.0}
            )
        )
        tool = SendMessageTool(bot)
        message = FakeMessage()
        result = await tool.execute(message, content="hello there")
        assert "__MESSAGE_SENT__" in result
        assert "hello there" in result
        # Suffix is user-facing only — not echoed back as tool prose.
        assert "ttft" not in result
        assert len(message.replies) == 1
        assert message.replies[0].startswith("hello there")
        assert "ttft 200ms" in message.replies[0]
        assert "50.0 tps" in message.replies[0]

    asyncio.run(run())


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

        def _is_admin(self, _uid):
            return True

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

        # An explicit invalid destination must not leak content into this chat.
        res3 = await tool.execute(origin_msg, content="nope", channel_id="invalid_id")
        assert res3.startswith("Error:")
        assert origin_msg.replies == []

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


def test_send_message_cross_channel_refuses_non_admin():
    class Target:
        def __init__(self):
            self.id = 123456789
            self.sent = []

        async def send(self, text):
            self.sent.append(text)

    class Bot:
        def __init__(self):
            self.target = Target()

        def get_channel(self, cid):
            return self.target if cid == 123456789 else None

        def _is_admin(self, _uid):
            return False

    async def run():
        bot = Bot()
        msg = FakeMessage(author_id=1003210843984498748, channel_id=99)
        result = await SendMessageTool(bot).execute(
            msg, content="its just me no one else", channel_id="123456789"
        )
        assert result.startswith("Error:")
        assert "admin" in result.lower()
        assert bot.target.sent == []
        assert msg.replies == []
        assert msg.channel.sent == []

    asyncio.run(run())


def test_send_message_same_channel_id_allowed_for_non_admin():
    async def run():
        msg = FakeMessage(author_id=11, channel_id=99)
        bot = SimpleNamespace(_is_admin=lambda _uid: False)
        result = await SendMessageTool(bot).execute(
            msg, content="hello here", channel_id="99"
        )
        assert "__MESSAGE_SENT__" in result
        assert msg.replies == ["hello here"]

    asyncio.run(run())


def test_send_message_cross_channel_from_dm_refuses_even_admin():
    class Target:
        def __init__(self):
            self.id = 123456789
            self.sent = []
            self.guild = SimpleNamespace(id=2)

        async def send(self, text):
            self.sent.append(text)

    class Bot:
        def __init__(self):
            self.target = Target()

        def get_channel(self, cid):
            return self.target if cid == 123456789 else None

        def _is_admin(self, _uid):
            return True

    async def run():
        bot = Bot()
        msg = FakeMessage(author_id=42, channel_id=99, dm=True)
        result = await SendMessageTool(bot).execute(
            msg, content="post this in general", channel_id="123456789"
        )
        assert result.startswith("Error:")
        assert "dm" in result.lower()
        assert bot.target.sent == []
        assert msg.replies == []
        assert msg.channel.sent == []

    asyncio.run(run())


def test_send_message_same_dm_recipient_allowed_for_non_admin():
    async def run():
        msg = FakeMessage(author_id=11, channel_id=1546631263928979457, dm=True)
        msg.channel.recipient = SimpleNamespace(id=1003210843984498748)
        bot = SimpleNamespace(
            _is_admin=lambda _uid: False,
            get_user=lambda _cid: None,
            get_channel=lambda _cid: None,
        )

        async def fetch_user(uid):
            raise RuntimeError("should not fetch")

        bot.fetch_user = fetch_user
        bot.fetch_channel = fetch_user
        result = await SendMessageTool(bot).execute(
            msg, content="ok", user_id="1003210843984498748"
        )
        # Same DM recipient is current chat, so it still sends here.
        assert "__MESSAGE_SENT__" in result
        assert msg.replies == ["ok"]

    asyncio.run(run())


def test_prepare_tool_params_maps_message_alias_to_content():
    from bot import _prepare_tool_params

    assert _prepare_tool_params("send_message", {"message": "hi"})["content"] == "hi"
    assert "message" not in _prepare_tool_params("send_message", {"message": "hi"})
    kept = _prepare_tool_params("send_message", {"content": "keep", "message": "drop"})
    assert kept["content"] == "keep"
    assert "message" not in kept
    assert _prepare_tool_params("send_message", {"text": "yo"})["content"] == "yo"
    # Other tools: just drop the colliding key, don't rename it.
    shell = _prepare_tool_params("shell", {"command": "ls", "message": "nope"})
    assert shell == {"command": "ls"}


def test_send_message_does_not_forward_stray_kwargs_to_discord():
    class Chan:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kwargs):
            if "content" in kwargs:
                raise TypeError("got multiple values for keyword argument 'content'")
            stray = set(kwargs) - {"stickers"}
            if stray:
                raise TypeError(f"stray kwargs {stray}")
            self.sent.append(text)

    class Msg:
        def __init__(self):
            self.channel = Chan()
            self.replies = []

        async def reply(self, text, **kwargs):
            if "content" in kwargs:
                raise TypeError("got multiple values for keyword argument 'content'")
            stray = set(kwargs) - {"stickers"}
            if stray:
                raise TypeError(f"stray kwargs {stray}")
            self.replies.append(text)

    async def run():
        tool = SendMessageTool(SimpleNamespace())
        msg = Msg()
        result = await tool.execute(
            msg, content="hi", reasoning="think", tts=True, body="nope"
        )
        assert "__MESSAGE_SENT__" in result
        assert msg.replies == ["hi"]

    asyncio.run(run())


def test_send_receipt_is_recorded_only_after_success():
    async def run():
        events = []
        message = FakeMessage()
        receipt = SimpleNamespace(id=90)

        async def send(_channel, content, **_kwargs):
            events.append(("send", content))
            return receipt

        bot = SimpleNamespace(
            _send_with_slowmode=send,
            _mark_request_effect=lambda msg: events.append(("effect", msg)),
            _record_delivery=lambda msg, sent: events.append(("delivered", sent.id)),
        )
        result = await SendMessageTool(bot).execute(message, content="answer")
        assert "__MESSAGE_SENT__" in result
        assert events == [
            ("effect", message),
            ("send", "answer"),
            ("delivered", 90),
        ]

    asyncio.run(run())


def test_send_none_does_not_claim_delivery():
    async def run():
        receipts = []

        async def send(*_args, **_kwargs):
            return None

        bot = SimpleNamespace(
            _send_with_slowmode=send,
            _record_delivery=lambda *_args: receipts.append("delivered"),
        )
        result = await SendMessageTool(bot).execute(FakeMessage(), content="answer")
        assert result.startswith("Error:")
        assert "__MESSAGE_SENT__" not in result
        assert receipts == []

    asyncio.run(run())


def test_partial_send_does_not_claim_unsent_chunks():
    async def run():
        calls = 0
        outcomes = []

        async def send(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(id=90) if calls == 1 else None

        bot = SimpleNamespace(
            _send_with_slowmode=send,
            _record_request_outcome=lambda _msg, status, **kw: outcomes.append(
                (status, kw.get("reason"))
            ),
        )
        result = await SendMessageTool(bot).execute(
            FakeMessage(), content="a" * 1900 + "b" * 100
        )
        assert result == "__MESSAGE_SENT__\n" + "a" * 1900
        assert outcomes == [("delivered", "partial_delivery")]
        assert calls == 2

    asyncio.run(run())


def test_failed_effect_checkpoint_prevents_send():
    async def run():
        message = FakeMessage()

        def mark(_message):
            raise OSError("journal unavailable")

        result = await SendMessageTool(
            SimpleNamespace(_mark_request_effect=mark)
        ).execute(message, content="answer")
        assert result.startswith("Error")
        assert message.replies == []
        assert message.channel.sent == []

    asyncio.run(run())
