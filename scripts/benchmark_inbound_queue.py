#!/usr/bin/env python3
"""Microbenchmark bounded inbound reply admission without Discord or providers.

The unbounded reference uses a global capacity above the offered workload to
model the pre-cap ReplyQueue. Handlers block on an Event so the benchmark holds
accepted work in memory rather than measuring mock inference throughput.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import math
import platform
import statistics
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from message_pipeline import ReplyQueue  # noqa: E402


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "p99_ms": round(_percentile(values, 0.99), 3),
    }


def _messages(count: int, scenario: str):
    for index in range(count):
        guild_id = str(index) if scenario == "distributed" else "hot-guild"
        channel_id = str(index) if scenario != "hot-channel" else "hot-channel"
        message = SimpleNamespace(
            id=str(index),
            guild=SimpleNamespace(id=guild_id),
            channel=SimpleNamespace(id=channel_id),
            author=SimpleNamespace(id=str(index % 100)),
        )
        yield guild_id, channel_id, message


async def _sample(
    count: int, *, scenario: str, capacity: int
) -> dict[str, float | int]:
    gate = asyncio.Event()
    started = 0
    all_started = asyncio.Event()
    dropped = 0
    per_submit_ms: list[float] = []

    async def handler(_message, _content):
        nonlocal started
        started += 1
        if started == expected:
            all_started.set()
        await gate.wait()

    def on_drop(_cid, _entry, reason):
        nonlocal dropped
        if reason == "deferred":
            dropped += 1

    queue = ReplyQueue(
        max_directed=8,
        max_outstanding=capacity,
        on_drop=on_drop,
    )
    queue.bind(handler)
    expected = 0
    accepted = 0
    accepted_channels: set[str] = set()
    loop = asyncio.get_running_loop()
    loop_lag = loop.create_future()
    tracemalloc.start()
    gc.collect()
    base_bytes = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()

    loop_scheduled_at = loop.time()
    loop.call_soon(
        lambda: loop_lag.set_result((loop.time() - loop_scheduled_at) * 1000)
    )
    start_ns = time.perf_counter_ns()
    for _guild_id, channel_id, message in _messages(count, scenario):
        before = time.perf_counter_ns()
        outcome = queue.submit(
            channel_id, message, "synthetic directed message", directed=True
        )
        per_submit_ms.append((time.perf_counter_ns() - before) / 1_000_000)
        if outcome in {"started", "queued"}:
            accepted += 1
            accepted_channels.add(channel_id)
    submit_ns = max(1, time.perf_counter_ns() - start_ns)
    # Per-channel order means only one handler is running for a burst in one
    # room even when several directed entries are safely retained behind it.
    expected = len(accepted_channels)

    if accepted:
        await asyncio.wait_for(all_started.wait(), timeout=5)
    else:
        await asyncio.sleep(0)
    await loop_lag

    retained_tasks = max(0, len(asyncio.all_tasks()) - 1)
    stats = queue.stats()
    _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result = {
        "offered": count,
        "admitted": accepted,
        "deferred": dropped,
        "retained": int(stats["outstanding"]),
        "channel_states": int(stats["channels_tracked"]),
        "pending_tasks": retained_tasks,
        "enqueue_throughput_per_s": round(count / (submit_ns / 1_000_000_000), 1),
        "admission_latency": _summary(per_submit_ms),
        "event_loop_lag_ms": round(await loop_lag, 3),
        "tracemalloc_peak_delta_kib": round(max(0, peak_bytes - base_bytes) / 1024, 1),
    }
    await queue.close()
    return result


async def _run(args):
    results = []
    for servers in args.servers:
        for scenario in ("distributed", "single-guild", "hot-channel"):
            offered = servers
            reference_samples = []
            bounded_samples = []
            reference_capacity = max(servers + 1, args.max_outstanding + 1)
            for _ in range(args.iterations):
                reference_samples.append(
                    await _sample(
                        offered, scenario=scenario, capacity=reference_capacity
                    )
                )
                bounded_samples.append(
                    await _sample(
                        offered, scenario=scenario,
                        capacity=args.max_outstanding,
                    )
                )

            def median(field, rows):
                return round(statistics.median(row[field] for row in rows), 3)

            results.append({
                "servers": servers,
                "scenario": scenario,
                "reference": {
                    "admitted": median("admitted", reference_samples),
                    "deferred": median("deferred", reference_samples),
                    "pending_tasks": median("pending_tasks", reference_samples),
                    "enqueue_throughput_per_s": median(
                        "enqueue_throughput_per_s", reference_samples
                    ),
                    "admission_latency_p50_p95_p99_ms": {
                        key: median_nested(key, reference_samples)
                        for key in ("p50_ms", "p95_ms", "p99_ms")
                    },
                    "event_loop_lag_ms": median("event_loop_lag_ms", reference_samples),
                    "tracemalloc_peak_delta_kib": median(
                        "tracemalloc_peak_delta_kib", reference_samples
                    ),
                },
                "bounded": {
                    "admitted": median("admitted", bounded_samples),
                    "deferred": median("deferred", bounded_samples),
                    "pending_tasks": median("pending_tasks", bounded_samples),
                    "enqueue_throughput_per_s": median(
                        "enqueue_throughput_per_s", bounded_samples
                    ),
                    "admission_latency_p50_p95_p99_ms": {
                        key: median_nested(key, bounded_samples)
                        for key in ("p50_ms", "p95_ms", "p99_ms")
                    },
                    "event_loop_lag_ms": median("event_loop_lag_ms", bounded_samples),
                    "tracemalloc_peak_delta_kib": median(
                        "tracemalloc_peak_delta_kib", bounded_samples
                    ),
                },
            })
    print(json.dumps({
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "iterations": args.iterations,
            "global_capacity": args.max_outstanding,
            "discord": False,
            "provider": None,
            "notes": "queue and task retention only; not bot or inference capacity",
        },
        "results": results,
    }, indent=2))


def median_nested(key: str, rows: list[dict]) -> float:
    return round(statistics.median(
        row["admission_latency"][key] for row in rows
    ), 3)


def main():
    logging.getLogger("message_pipeline").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--servers", nargs="+", type=int, default=[100, 500, 1000])
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--max-outstanding", type=int, default=256)
    args = parser.parse_args()
    if args.iterations < 1 or args.max_outstanding < 1:
        parser.error("iterations and max-outstanding must be positive")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
