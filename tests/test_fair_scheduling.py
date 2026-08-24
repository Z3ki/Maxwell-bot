"""Many rooms, few slots: nobody gets starved and locks don't accumulate."""

import asyncio

import pytest

from concurrency_safety import FairSemaphore, KeyedLocks, ToolConcurrency, classify_tool


def test_one_busy_room_cannot_monopolise_the_pool():
    """The scenario this exists for: one guild floods while others wait."""
    sem = FairSemaphore(1)
    served: list[str] = []

    async def caller(key, delay=0.0):
        if delay:
            await asyncio.sleep(delay)
        await sem.acquire(5, key=key, priority="user")
        served.append(key)
        await asyncio.sleep(0.01)
        await sem.release()

    async def run():
        # "loud" queues four turns before the two quiet rooms say anything.
        await asyncio.gather(
            *[caller("loud") for _ in range(4)],
            caller("quiet-a", 0.001),
            caller("quiet-b", 0.001),
        )

    asyncio.run(run())
    assert len(served) == 6
    # Neither quiet room waits behind all four of the loud room's turns.
    assert served.index("quiet-a") < 4
    assert served.index("quiet-b") < 5


def test_a_person_outranks_a_background_tick():
    sem = FairSemaphore(1)
    order: list[str] = []

    async def run():
        await sem.acquire(5, key="holder", priority="user")

        async def queued(name, priority):
            await sem.acquire(5, key=name, priority=priority)
            order.append(name)
            await sem.release()

        tasks = [
            asyncio.create_task(queued("autonomy", "background")),
            asyncio.create_task(queued("rem", "background")),
        ]
        await asyncio.sleep(0.01)
        tasks.append(asyncio.create_task(queued("person", "user")))
        await asyncio.sleep(0.01)
        await sem.release()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    # The person arrived last and still goes first.
    assert order[0] == "person"


def test_waiting_gives_up_at_the_deadline_without_wedging_the_queue():
    sem = FairSemaphore(1)

    async def run():
        await sem.acquire(5, key="held", priority="user")
        with pytest.raises(asyncio.TimeoutError):
            await sem.acquire(0.02, key="waiter", priority="user")
        # The timed-out waiter left cleanly; the next caller still gets in.
        assert sem.waiting == 0
        await sem.release()
        await sem.acquire(1, key="next", priority="user")
        assert sem.active == 1

    asyncio.run(run())


def test_a_cancelled_waiter_does_not_block_whoever_is_behind_it():
    sem = FairSemaphore(1)
    got = []

    async def run():
        await sem.acquire(5, key="held", priority="user")
        doomed = asyncio.create_task(sem.acquire(5, key="a", priority="user"))
        await asyncio.sleep(0.01)
        behind = asyncio.create_task(sem.acquire(5, key="b", priority="user"))
        await asyncio.sleep(0.01)
        doomed.cancel()
        await asyncio.sleep(0)
        await sem.release()
        await behind
        got.append("b")
        assert sem.waiting == 0

    asyncio.run(run())
    assert got == ["b"]


def test_capacity_can_be_raised_at_runtime():
    sem = FairSemaphore(1)

    async def run():
        await sem.acquire(5, key="a", priority="user")
        blocked = asyncio.create_task(sem.acquire(5, key="b", priority="user"))
        await asyncio.sleep(0.01)
        assert not blocked.done()
        await sem.set_capacity(2)
        await blocked
        assert sem.active == 2

    asyncio.run(run())


def test_served_history_stays_bounded():
    sem = FairSemaphore(4, history=32)

    async def run():
        for n in range(200):
            await sem.acquire(1, key=f"room-{n}", priority="user")
            await sem.release()

    asyncio.run(run())
    assert sem.stats()["known_keys"] <= 32


def test_locks_are_reclaimed_but_never_stolen_while_held():
    locks = KeyedLocks(max_idle=16)

    async def run():
        held = locks.get("busy")
        await held.acquire()
        for n in range(200):
            locks.get(f"cold-{n}")
        assert len(locks) <= 32
        # The held lock is still the same object — pruning it would let two
        # turns into that room at once.
        assert locks.get("busy") is held
        held.release()

    asyncio.run(run())


def test_prune_keeps_live_rooms():
    locks = KeyedLocks(max_idle=16)

    async def run():
        for n in range(10):
            locks.get(f"room-{n}")
        removed = locks.prune(keep={"room-1", "room-2"}, all_idle=True)
        assert removed == 8
        assert "room-1" in locks and "room-2" in locks

    asyncio.run(run())


def test_the_same_room_gets_the_same_lock():
    locks = KeyedLocks()

    async def run():
        assert locks.get("a") is locks.get("a")
        assert locks.get("a") is not locks.get("b")

    asyncio.run(run())


def test_heavy_tools_draw_on_separate_budgets():
    assert classify_tool("image_generator") == "media"
    assert classify_tool("tts") == "tts"
    assert classify_tool("shell") == "shell"
    assert classify_tool("create_site") == "site"
    assert classify_tool("fetch_url") == "web"
    assert classify_tool("send_message") == "default"


def test_a_tool_can_declare_its_own_budget():
    class Custom:
        concurrency_class = "media"

    assert classify_tool("send_message", Custom()) == "media"


def test_a_saturated_budget_queues_instead_of_piling_up():
    budgets = ToolConcurrency(media=1)
    running = {"n": 0, "peak": 0}

    async def work():
        async with budgets.slot("media"):
            running["n"] += 1
            running["peak"] = max(running["peak"], running["n"])
            await asyncio.sleep(0.01)
            running["n"] -= 1

    async def run():
        await asyncio.gather(*[work() for _ in range(6)])

    asyncio.run(run())
    assert running["peak"] == 1
    # And the cheap budget really does run wide.
    assert budgets.stats()["default"]["limit"] == 16
