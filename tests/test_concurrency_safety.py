import asyncio
import time

import pytest

from concurrency_safety import ChannelWorkQueues, ToolConcurrency, offload


def test_sync_work_is_offloaded():
    async def run():
        started = asyncio.Event()

        def blocking():
            started.set()
            time.sleep(0.02)
            return 42

        task = asyncio.create_task(offload(blocking))
        await started.wait()
        return await task

    assert asyncio.run(run()) == 42


def test_channels_do_not_block_each_other():
    async def run():
        queues = ChannelWorkQueues(max_pending=2)
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return "slow"

        slow_task = asyncio.create_task(queues.submit(1, 1, slow))
        await asyncio.sleep(0)
        fast = await queues.submit(2, 2, lambda: asyncio.sleep(0, result="fast"))
        release.set()
        assert await slow_task == "slow"
        await queues.close()
        return fast

    assert asyncio.run(run()) == "fast"


def test_queue_is_bounded():
    async def run():
        queues = ChannelWorkQueues(max_pending=1)
        blocker = asyncio.Event()

        async def work():
            await blocker.wait()

        first = asyncio.create_task(queues.submit(1, 1, work))
        await asyncio.sleep(0)
        second = asyncio.create_task(queues.submit(1, 1, work))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="queue is full"):
            await queues.submit(1, 1, work)
        blocker.set()
        first.cancel(); second.cancel()
        await queues.close()

    asyncio.run(run())


def test_tool_gate_enforces_deadline():
    async def run():
        gates = ToolConcurrency(provider=1)
        with pytest.raises(asyncio.TimeoutError):
            await gates.run("provider", asyncio.sleep(10), timeout=0.001)

    asyncio.run(run())
