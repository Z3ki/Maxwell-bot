"""Sub-agent event streaming.

The rule this module lives by: telemetry must never pace, block or kill the
work it is describing. Most of these tests are that rule stated three
different ways.
"""

import asyncio

import agent_events
from agent_events import (
    EV_STEP,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    AgentEventBus,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_a_run_records_its_own_lifecycle():
    bus = AgentEventBus()
    run = bus.start_run("build a parser", requested_by="alice", max_steps=24)
    bus.publish(run.run_id, EV_STEP, step=1, label="step 1/24")
    bus.finish_run(run.run_id, STATUS_DONE, "done")

    kinds = [e["type"] for e in bus.events(run.run_id)]
    assert kinds == ["start", "step", "finish"]
    snapshot = bus.snapshot()[0]
    assert snapshot["status"] == STATUS_DONE
    assert snapshot["task"] == "build a parser"
    assert snapshot["requested_by"] == "alice"
    assert snapshot["summary"] == "done"


def test_a_live_subscriber_sees_events_as_they_happen():
    async def run():
        bus = AgentEventBus()
        agent = bus.start_run("x")
        seen = []

        async def watcher():
            async for event in bus.stream(agent.run_id):
                seen.append(event.data.get("label") or event.type)

        task = asyncio.create_task(watcher())
        await asyncio.sleep(0)
        bus.publish(agent.run_id, EV_STEP, label="one")
        bus.publish(agent.run_id, EV_STEP, label="two")
        bus.finish_run(agent.run_id, STATUS_DONE, "fin")
        # The finish sentinel is what ends the stream — without it a watcher
        # would block forever on a queue that will never fill again.
        await asyncio.wait_for(task, timeout=3)
        assert seen == ["one", "two", "finish"]

    _run(run())


def test_a_stalled_subscriber_never_blocks_the_agent():
    # The queue is bounded and nobody is reading it. Publishing must still
    # return promptly, dropping the oldest events rather than applying
    # backpressure to the run.
    bus = AgentEventBus()
    run = bus.start_run("x")
    queue = bus.subscribe(run.run_id)
    for i in range(agent_events.SUBSCRIBER_QUEUE_SIZE * 3):
        assert bus.publish(run.run_id, EV_STEP, step=i) is not None
    assert queue.qsize() == agent_events.SUBSCRIBER_QUEUE_SIZE


def test_history_is_bounded():
    bus = AgentEventBus()
    run = bus.start_run("x")
    for i in range(agent_events.MAX_EVENTS_PER_RUN * 2):
        bus.publish(run.run_id, EV_STEP, step=i)
    assert len(bus.get(run.run_id).events) == agent_events.MAX_EVENTS_PER_RUN


def test_publishing_to_an_unknown_run_is_ignored():
    bus = AgentEventBus()
    assert bus.publish("nope", EV_STEP) is None
    assert bus.events("nope") == []
    assert bus.subscribe("nope") is None
    assert bus.get("nope") is None
    bus.finish_run("nope")  # must not raise


def test_events_can_be_read_incrementally():
    bus = AgentEventBus()
    run = bus.start_run("x")
    bus.publish(run.run_id, EV_STEP, step=1)
    first = bus.events(run.run_id)
    bus.publish(run.run_id, EV_STEP, step=2)
    later = bus.events(run.run_id, since_seq=first[-1]["seq"])
    assert [e["step"] for e in later] == [2]


def test_finished_runs_are_evicted_but_running_ones_are_not():
    bus = AgentEventBus()
    live = bus.start_run("still going")
    for i in range(agent_events.MAX_FINISHED_RUNS + 10):
        done = bus.start_run(f"run {i}")
        bus.finish_run(done.run_id, STATUS_DONE)
    ids = [r["run_id"] for r in bus.snapshot()]
    assert live.run_id in ids
    assert bus.stats()["running"] == 1
    assert bus.stats()["finished"] <= agent_events.MAX_FINISHED_RUNS


def test_running_runs_sort_ahead_of_finished_ones():
    bus = AgentEventBus()
    old = bus.start_run("finished first")
    bus.finish_run(old.run_id, STATUS_DONE)
    live = bus.start_run("running now")
    assert bus.snapshot()[0]["run_id"] == live.run_id
    assert bus.snapshot(include_finished=False) == [
        r for r in bus.snapshot() if r["status"] == STATUS_RUNNING
    ]


def test_a_failed_run_is_reported_as_an_error_event():
    bus = AgentEventBus()
    run = bus.start_run("x")
    bus.finish_run(run.run_id, STATUS_FAILED, "provider exploded")
    assert bus.events(run.run_id)[-1]["type"] == "error"
    assert bus.snapshot()[0]["status"] == STATUS_FAILED


def test_the_change_hook_fires_but_cannot_break_a_run():
    calls = []
    bus = AgentEventBus(on_change=lambda b: calls.append(len(b.snapshot())))
    run = bus.start_run("x")
    bus.publish(run.run_id, EV_STEP, step=1)
    assert calls

    exploding = AgentEventBus(on_change=lambda b: 1 / 0)
    boom = exploding.start_run("x")
    assert exploding.publish(boom.run_id, EV_STEP) is not None
    exploding.finish_run(boom.run_id, STATUS_DONE)


def test_bus_for_ignores_objects_without_one():
    import types

    bus = AgentEventBus()
    assert agent_events.bus_for(types.SimpleNamespace(agent_events=bus)) is bus
    assert agent_events.bus_for(types.SimpleNamespace()) is None
    assert agent_events.bus_for(types.SimpleNamespace(agent_events="nope")) is None
