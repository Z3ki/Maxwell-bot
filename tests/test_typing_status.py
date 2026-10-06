"""Channel typing lifecycle.

The contract these tests pin down: typing is ref-counted per channel, it
is drained BEFORE the bot posts visible output (so no typing event can
trail the message and re-light the indicator), and it only comes back
when a new generation step resumes work.
"""

import asyncio
from types import SimpleNamespace

from typing_status import (
    typing_acquire,
    typing_release,
    typing_resume,
    typing_suspend,
)


class FakeTyping:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append("enter")
        return self

    async def __aexit__(self, *args):
        self.log.append("exit")
        return False


class FakeChannel:
    def __init__(self, channel_id="chan"):
        self.id = channel_id
        self.log = []

    def typing(self):
        return FakeTyping(self.log)


def test_refcounted_acquires_share_one_loop():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        first = await typing_acquire(owner, channel)
        second = await typing_acquire(owner, channel)
        assert channel.log == ["enter"]
        await typing_release(first)
        assert channel.log == ["enter"]
        await typing_release(second)
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())


def test_suspend_drains_before_the_send_and_blocks_relighting():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        handle = await typing_acquire(owner, channel)
        assert channel.log == ["enter"]
        await typing_suspend(owner, channel)
        assert channel.log == ["enter", "exit"]
        late = await typing_acquire(owner, channel)
        assert channel.log == ["enter", "exit"]
        await typing_release(late)
        await typing_release(handle)
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())


def test_resume_relights_only_while_a_holder_remains():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        await typing_resume(owner, channel)
        assert channel.log == []
        handle = await typing_acquire(owner, channel)
        await typing_suspend(owner, channel)
        assert channel.log == ["enter", "exit"]
        await typing_resume(owner, channel)
        assert channel.log == ["enter", "exit", "enter"]
        await typing_release(handle)
        assert channel.log == ["enter", "exit", "enter", "exit"]

    asyncio.run(run())


def test_release_is_idempotent():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        handle = await typing_acquire(owner, channel)
        await typing_release(handle)
        await typing_release(handle)
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())


def test_channels_are_isolated_per_owner_and_channel():
    async def run():
        owner = SimpleNamespace()
        other = SimpleNamespace()
        channel = FakeChannel("a")
        sibling = FakeChannel("b")
        handle = await typing_acquire(owner, channel)
        await typing_acquire(owner, sibling)
        await typing_acquire(other, FakeChannel("a"))
        await typing_suspend(owner, channel)
        assert channel.log == ["enter", "exit"]
        assert sibling.log == ["enter"]
        await typing_release(handle)

    asyncio.run(run())
