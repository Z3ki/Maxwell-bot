"""Live event streaming for sub-agent runs.

A sub-agent run is a loop that can go for minutes: the model plans, runs a
command, reads the output, writes a file, tries again. Until now the only
thing anyone saw was the final report — the channel went quiet for four
minutes and came back with a wall of text. If it hung, there was nothing to
look at; if it failed on step 19 of 24, you found out at the end.

This module is the plumbing that fixes that: an in-process event bus where a
run publishes what it is doing as it does it, and any number of consumers
read it live. Two consumers exist today — the channel progress message the
user watches, and the admin dashboard — and they want different things, which
is why this is a bus and not a callback.

Design notes
------------
**Bounded everywhere.** A run's history is a bounded deque and each
subscriber gets a bounded queue. A subscriber that stops reading (a closed
dashboard tab, a Discord edit that is rate-limited) drops its oldest events
rather than growing without limit or — much worse — applying backpressure to
the agent. The agent must never block on someone watching it.

**Publishing never raises.** A run that dies because its telemetry failed
would be a bad trade. Every publish path swallows its own errors.

**In-process only.** Runs live in the bot process and die with it; there is
no persistence and none is wanted. The dashboard reads live state, and the
durable record of what a sub-agent did is its report in the channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Events kept per run for late subscribers and the dashboard. A 24-step run
# emits roughly 4 events per step, so this holds several runs' worth of
# detail without ever being a memory concern.
MAX_EVENTS_PER_RUN = 400

# Depth of one subscriber's queue. Deeper than a burst of events from one
# step, shallow enough that a dead subscriber is not holding much.
SUBSCRIBER_QUEUE_SIZE = 200

# Finished runs kept for inspection after they end. The dashboard wants to
# show the last few; beyond that the report in the channel is the record.
MAX_FINISHED_RUNS = 25

# Event types. Kept as plain strings — consumers filter on them and one of
# those consumers is JavaScript.
EV_START = "start"
EV_STEP = "step"
EV_TOOL_CALL = "tool_call"
EV_TOOL_RESULT = "tool_result"
EV_NOTE = "note"
EV_FINISH = "finish"
EV_ERROR = "error"

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentEvent:
    """One thing that happened during a run."""

    run_id: str
    seq: int
    ts: str
    type: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            **self.data,
        }


@dataclass
class AgentRun:
    """One sub-agent invocation, from start to report."""

    run_id: str
    task: str
    status: str = STATUS_RUNNING
    started_at: str = field(default_factory=_utcnow_iso)
    finished_at: str = ""
    started_monotonic: float = field(default_factory=time.monotonic)
    requested_by: str = ""
    channel_id: str = ""
    workdir: str = ""
    steps: int = 0
    max_steps: int = 0
    commands_run: int = 0
    files_written: list[str] = field(default_factory=list)
    last_activity: str = ""
    summary: str = ""
    events: deque[AgentEvent] = field(
        default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_RUN)
    )
    _seq: int = 0
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    def as_dict(self, include_events: bool = False) -> dict:
        out = {
            "run_id": self.run_id,
            "task": self.task,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.elapsed, 1),
            "requested_by": self.requested_by,
            "channel_id": self.channel_id,
            "workdir": self.workdir,
            "steps": self.steps,
            "max_steps": self.max_steps,
            "commands_run": self.commands_run,
            "files_written": list(self.files_written),
            "last_activity": self.last_activity,
            "summary": self.summary,
            "event_count": len(self.events),
            "watchers": len(self._subscribers),
        }
        if include_events:
            out["events"] = [e.as_dict() for e in self.events]
        return out


class AgentEventBus:
    """Live registry of sub-agent runs and their events.

    One instance per bot, hung off ``bot.agent_events``. Everything is
    synchronous except ``stream``: publishing from inside the agent loop must
    not await anything, or a slow watcher would pace the agent.
    """

    def __init__(self, on_change=None):
        self._runs: dict[str, AgentRun] = {}
        self._order: deque[str] = deque()
        # Called (synchronously, never awaited) after anything changes. The
        # bot uses it to mirror the live snapshot to disk for the dashboard,
        # which runs in a separate process and cannot see this object. Kept
        # as a plain callback so this module owes nothing to the bot.
        self._on_change = on_change

    def _changed(self):
        if self._on_change is None:
            return
        try:
            self._on_change(self)
        except Exception as e:  # pragma: no cover - a mirror must not kill a run
            logger.debug("agent event on_change failed: %s", e)

    # ─── lifecycle ────────────────────────────────────────────────────

    def start_run(
        self,
        task: str,
        requested_by: str = "",
        channel_id: str = "",
        workdir: str = "",
        max_steps: int = 0,
    ) -> AgentRun:
        run = AgentRun(
            run_id=uuid.uuid4().hex[:12],
            task=" ".join(str(task or "").split())[:500],
            requested_by=str(requested_by or "")[:80],
            channel_id=str(channel_id or ""),
            workdir=str(workdir or ""),
            max_steps=max(0, int(max_steps or 0)),
        )
        self._runs[run.run_id] = run
        self._order.append(run.run_id)
        self._evict_finished()
        self.publish(run.run_id, EV_START, task=run.task, max_steps=run.max_steps)
        return run

    def finish_run(self, run_id: str, status: str = STATUS_DONE, summary: str = ""):
        run = self._runs.get(run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = _utcnow_iso()
        run.summary = str(summary or "")[:2000]
        self.publish(
            run_id,
            EV_FINISH if status == STATUS_DONE else EV_ERROR,
            status=status,
            summary=run.summary,
            steps=run.steps,
            commands_run=run.commands_run,
        )
        # Wake every watcher so a `stream` consumer can notice the run ended
        # instead of blocking on a queue that will never fill again.
        for queue in list(run._subscribers):
            with contextlib.suppress(Exception):
                queue.put_nowait(None)
        self._evict_finished()
        self._changed()

    def _evict_finished(self):
        """Drop the oldest finished runs. Running ones are never evicted."""
        finished = [
            rid
            for rid in self._order
            if (self._runs.get(rid) or AgentRun("", "")).status != STATUS_RUNNING
        ]
        for rid in finished[:-MAX_FINISHED_RUNS] if len(finished) > MAX_FINISHED_RUNS else []:
            self._runs.pop(rid, None)
            with contextlib.suppress(ValueError):
                self._order.remove(rid)

    # ─── publish ──────────────────────────────────────────────────────

    def publish(self, run_id: str, event_type: str, **data) -> AgentEvent | None:
        """Record an event and fan it out. Never raises, never blocks.

        A watcher whose queue is full loses its oldest event rather than
        stalling the agent — telemetry is not worth pausing real work for.
        """
        run = self._runs.get(run_id)
        if run is None:
            return None
        try:
            run._seq += 1
            event = AgentEvent(
                run_id=run_id,
                seq=run._seq,
                ts=_utcnow_iso(),
                type=str(event_type),
                data={k: v for k, v in data.items() if v is not None},
            )
            run.events.append(event)
            if event_type in (EV_STEP, EV_TOOL_CALL, EV_NOTE):
                run.last_activity = str(
                    data.get("label") or data.get("tool") or event_type
                )[:200]
            for queue in list(run._subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(event)
                except Exception:
                    pass
            self._changed()
            return event
        except Exception as e:  # pragma: no cover - telemetry must not kill a run
            logger.debug("agent event publish failed: %s", e)
            return None

    # ─── consume ──────────────────────────────────────────────────────

    def subscribe(self, run_id: str) -> asyncio.Queue | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        run._subscribers.append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue):
        run = self._runs.get(run_id)
        if run is None:
            return
        with contextlib.suppress(ValueError):
            run._subscribers.remove(queue)

    async def stream(self, run_id: str, timeout: float | None = None):
        """Async-iterate a run's live events until it finishes.

        Yields only events published *after* subscribing — a consumer that
        also wants the backlog should read `events()` first. Ends when the run
        finishes or `timeout` seconds pass with no event at all.
        """
        queue = self.subscribe(run_id)
        if queue is None:
            return
        try:
            while True:
                try:
                    event = (
                        await asyncio.wait_for(queue.get(), timeout=timeout)
                        if timeout
                        else await queue.get()
                    )
                except asyncio.TimeoutError:
                    return
                if event is None:  # sentinel from finish_run
                    return
                yield event
        finally:
            self.unsubscribe(run_id, queue)

    # ─── read ─────────────────────────────────────────────────────────

    def get(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id)

    def events(self, run_id: str, since_seq: int = 0) -> list[dict]:
        run = self._runs.get(run_id)
        if run is None:
            return []
        return [e.as_dict() for e in run.events if e.seq > since_seq]

    def snapshot(self, include_finished: bool = True, limit: int = 50) -> list[dict]:
        """Newest first, running runs before finished ones."""
        runs = [self._runs[rid] for rid in self._order if rid in self._runs]
        if not include_finished:
            runs = [r for r in runs if r.status == STATUS_RUNNING]
        runs.sort(key=lambda r: (r.status != STATUS_RUNNING, -r.started_monotonic))
        return [r.as_dict() for r in runs[: max(1, int(limit or 50))]]

    def stats(self) -> dict:
        running = sum(1 for r in self._runs.values() if r.status == STATUS_RUNNING)
        return {
            "runs": len(self._runs),
            "running": running,
            "finished": len(self._runs) - running,
        }


def bus_for(bot: Any) -> AgentEventBus | None:
    """The bot's bus, or None when the caller has no bot (tests, CLI)."""
    bus = getattr(bot, "agent_events", None)
    return bus if isinstance(bus, AgentEventBus) else None
