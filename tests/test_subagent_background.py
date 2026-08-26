"""Background (fire-and-forget) sub-agent mode.

mode=background must return immediately with a "started" ack and hand the
actual run to a background task that posts the result to the originating
channel when it finishes. The main turn is not blocked — that is the whole
point (Maxwell doesn't sit quiet for minutes while a nested agent works).
"""

import asyncio
import json

from bot_tools import SubAgentTool
from config import Config


def _call(name, args, call_id="1"):
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _ScriptedProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def generate_chat_completion(self, messages=None, **kwargs):
        self.seen.append(list(messages or []))
        if not self.replies:
            return {"role": "assistant", "content": "out of script", "tool_calls": []}
        return self.replies.pop(0)


class _FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.last_message = None

    async def send(self, content, **kwargs):
        msg = _FakeMessage(content)
        self.last_message = msg
        return msg


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.edits = []

    async def edit(self, content=None, **kwargs):
        self.edits.append(content)
        if content is not None:
            self.content = content


class _FakeUser:
    def __init__(self, uid, dm_channel):
        self.id = uid
        self.dm_channel = dm_channel

    async def create_dm(self):
        if self.dm_channel is None:
            self.dm_channel = _FakeChannel("dm-" + str(self.id))
        return self.dm_channel


class _FakeBot:
    def __init__(self, provider, channel, user=None):
        self.provider = provider
        self.channel = channel
        self.user = user
        self.agent_events = None

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    async def fetch_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    def get_user(self, uid):
        return self.user if self.user and self.user.id == uid else None

    async def fetch_user(self, uid):
        return self.user if self.user and self.user.id == uid else None


class _Message:
    def __init__(self, channel, author=None):
        self.channel = channel
        self.author = author or _FakeUser(1, None)


def _tool(provider, channel, tmp_path, monkeypatch, user=None):
    monkeypatch.setenv("SUBAGENT_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "SUBAGENT_BASE_DIR", str(tmp_path))
    # No daemon in tests; run commands directly on the host.
    monkeypatch.setattr(Config, "SUBAGENT_SANDBOX", "host")
    return SubAgentTool(_FakeBot(provider, channel, user=user))


def test_background_returns_immediately_and_posts_result(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    _call("write_file", {"path": "hi.py", "content": "print('hey')\n"})
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [_call("finish", {"report": "built and ran hi"}, "2")],
            },
        ]
    )
    chan = _FakeChannel(123)
    tool = _tool(provider, chan, tmp_path, monkeypatch)
    msg = _Message(chan)

    async def scenario():
        started = await tool.execute(
            msg, task="write a hello script", mode="background"
        )
        assert "Started sub-agent" in started
        # The background task shares this loop; let it run to completion.
        for _ in range(1000):
            if chan.last_message is not None and any(
                e and "done:" in e for e in chan.last_message.edits
            ):
                return chan.last_message.content
            await asyncio.sleep(0.01)
        raise AssertionError("background sub-agent never posted its result")

    content = asyncio.run(scenario())
    assert "built and ran hi" in (content or "")


def test_background_returns_immediately_even_without_a_channel(tmp_path, monkeypatch):
    # No sendable channel (message=None): the run must still be handed off and
    # complete without error, and execute() must still return the started ack.
    provider = _ScriptedProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [_call("finish", {"report": "did it"}, "1")],
            }
        ]
    )
    tool = _tool(provider, _FakeChannel(1), tmp_path, monkeypatch)

    async def scenario():
        started = await tool.execute(None, task="do a thing", mode="background")
        assert "Started sub-agent" in started
        # Let the background task drain. It has no channel to post to, so
        # completion is the only signal — poll the provider's seen history.
        for _ in range(1000):
            # finish() is the second reply; after it the provider is exhausted.
            if not provider.replies and provider.seen:
                return True
            await asyncio.sleep(0.01)
        raise AssertionError("background task did not complete")

    assert asyncio.run(scenario()) is True


def test_background_deliver_dm_posts_to_the_requester(tmp_path, monkeypatch):
    # deliver=dm must post the report to the author's DM channel, not to the
    # guild channel it was asked in (the "allow it to DM people" behaviour).
    provider = _ScriptedProvider(
        [
            {"role": "assistant", "tool_calls": [_call("finish", {"report": "hi from dm"}, "1")]}
        ]
    )
    chan = _FakeChannel(123)
    dm = _FakeChannel("dm-7")
    user = _FakeUser(7, dm)
    tool = _tool(provider, chan, tmp_path, monkeypatch, user=user)
    msg = _Message(chan, author=user)

    async def scenario():
        started = await tool.execute(msg, task="private task", mode="background", deliver="dm")
        assert "Started sub-agent" in started
        for _ in range(1000):
            if dm.last_message is not None and any(
                e and "done:" in e for e in dm.last_message.edits
            ):
                return dm.last_message.content
            await asyncio.sleep(0.01)
        raise AssertionError("background DM run never posted its result")

    content = asyncio.run(scenario())
    assert "hi from dm" in (content or "")
    # The guild channel must have received nothing.
    assert chan.last_message is None


def test_background_refuses_when_queue_is_full(tmp_path, monkeypatch):
    # A flood of heavy work across many channels must not grow the in-memory
    # queue without bound — once the queue is at its cap, new background
    # requests are refused with a clear message instead of piling up.
    provider = _ScriptedProvider(
        [{"role": "assistant", "tool_calls": [_call("finish", {"report": "x"}, "1")]}]
    )
    chan = _FakeChannel(123)
    tool = _tool(provider, chan, tmp_path, monkeypatch)
    # Simulate a saturated queue.
    tool._bg_max_queued = 1
    tool._bg_inflight = 1
    msg = _Message(chan)
    result = asyncio.run(tool.execute(msg, task="another job", mode="background"))
    assert "won't pile on more" in result
    # And it must not have enqueued anything nor started a run.
    assert provider.seen == []
    assert tool._bg_inflight == 1
