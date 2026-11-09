"""Channel typing lifecycle.

The contract these tests pin down: typing is ref-counted per channel, it
is drained BEFORE the bot posts visible output (so no typing event can
trail the message and re-light the indicator), and it only comes back
when a new generation step resumes work.
"""

import asyncio
from types import SimpleNamespace

import pytest

from typing_status import (
    status_for,
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


def test_concurrent_acquires_enter_only_one_typing_context():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        entered = asyncio.Event()
        proceed = asyncio.Event()
        second_started = asyncio.Event()
        contexts = []

        class SlowTyping(FakeTyping):
            async def __aenter__(self):
                entered.set()
                await proceed.wait()
                return await super().__aenter__()

        def typing():
            context = SlowTyping(channel.log)
            contexts.append(context)
            return context

        channel.typing = typing
        first = asyncio.create_task(typing_acquire(owner, channel))
        await entered.wait()

        async def acquire_second():
            second_started.set()
            return await typing_acquire(owner, channel)

        second = asyncio.create_task(acquire_second())
        await second_started.wait()
        proceed.set()
        handles = await asyncio.gather(first, second)
        assert len(contexts) == 1
        assert status_for(owner, channel).holders == 2
        assert channel.log == ["enter"]
        for handle in handles:
            await typing_release(handle)
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())


def test_suspend_waits_for_an_inflight_typing_entry_to_drain():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        entered = asyncio.Event()
        proceed = asyncio.Event()
        suspending = asyncio.Event()

        class SlowTyping(FakeTyping):
            async def __aenter__(self):
                entered.set()
                await proceed.wait()
                return await super().__aenter__()

        channel.typing = lambda: SlowTyping(channel.log)
        acquiring = asyncio.create_task(typing_acquire(owner, channel))
        await entered.wait()

        async def suspend():
            suspending.set()
            await typing_suspend(owner, channel)

        suspended = asyncio.create_task(suspend())
        await suspending.wait()
        assert not suspended.done()
        proceed.set()
        handle = await acquiring
        await suspended
        assert channel.log == ["enter", "exit"]
        assert not status_for(owner, channel).active
        await typing_release(handle)

    asyncio.run(run())


def test_cancelled_acquire_rolls_back_its_holder_and_drains_partial_entry():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        entered = asyncio.Event()
        running = asyncio.Event()
        drained = asyncio.Event()
        contexts = []

        class PartialTyping(FakeTyping):
            async def __aenter__(self):
                async def loop():
                    running.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        drained.set()

                self.task = asyncio.create_task(loop())
                await running.wait()
                entered.set()
                await asyncio.Event().wait()

        def typing():
            context = PartialTyping(channel.log)
            contexts.append(context)
            return context

        channel.typing = typing
        acquiring = asyncio.create_task(typing_acquire(owner, channel))
        await entered.wait()
        acquiring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquiring
        status = status_for(owner, channel)
        assert status.holders == 0
        assert not status.active
        assert drained.is_set()
        assert contexts[0].task.done()
        assert channel.log == ["exit"]

    asyncio.run(run())


def test_cancelled_release_still_drains_a_context_waiting_on_entry():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        held = await typing_acquire(owner, channel)
        await typing_suspend(owner, channel)
        entered = asyncio.Event()
        proceed = asyncio.Event()
        releasing = asyncio.Event()

        class SlowTyping(FakeTyping):
            async def __aenter__(self):
                entered.set()
                await proceed.wait()
                return await super().__aenter__()

        channel.typing = lambda: SlowTyping(channel.log)
        resuming = asyncio.create_task(typing_resume(owner, channel))
        await entered.wait()

        async def release():
            releasing.set()
            await typing_release(held)

        released = asyncio.create_task(release())
        await releasing.wait()
        released.cancel()
        proceed.set()
        await resuming
        with pytest.raises(asyncio.CancelledError):
            await released
        status = status_for(owner, channel)
        assert status.holders == 0
        assert not status.active
        assert channel.log == ["enter", "exit", "enter", "exit"]

    asyncio.run(run())


def test_release_does_not_cancel_typing_task_cleanup_twice():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        running = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        drained = asyncio.Event()

        class DrainingTyping(FakeTyping):
            async def __aenter__(self):
                async def loop():
                    running.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaning.set()
                        await finish_cleanup.wait()
                        drained.set()

                self.task = asyncio.create_task(loop())
                await running.wait()
                return await super().__aenter__()

            async def __aexit__(self, *args):
                self.task.cancel()
                await cleaning.wait()
                return await super().__aexit__(*args)

        channel.typing = lambda: DrainingTyping(channel.log)
        held = await typing_acquire(owner, channel)
        released = asyncio.create_task(typing_release(held))
        await cleaning.wait()
        finish_cleanup.set()
        await released
        assert drained.is_set()
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())


def test_cancelled_suspend_finishes_draining_before_losing_the_context():
    async def run():
        owner = SimpleNamespace()
        channel = FakeChannel()
        running = asyncio.Event()
        exiting = asyncio.Event()
        finish_exit = asyncio.Event()
        drained = asyncio.Event()
        contexts = []

        class SlowExitTyping(FakeTyping):
            async def __aenter__(self):
                async def loop():
                    running.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        drained.set()

                self.task = asyncio.create_task(loop())
                await running.wait()
                return await super().__aenter__()

            async def __aexit__(self, *args):
                exiting.set()
                await finish_exit.wait()
                return await super().__aexit__(*args)

        def typing():
            context = SlowExitTyping(channel.log)
            contexts.append(context)
            return context

        channel.typing = typing
        held = await typing_acquire(owner, channel)
        suspended = asyncio.create_task(typing_suspend(owner, channel))
        await exiting.wait()
        suspended.cancel()
        finish_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await suspended
        assert drained.is_set()
        assert contexts[0].task.done()
        assert not status_for(owner, channel).active
        await typing_release(held)
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())
