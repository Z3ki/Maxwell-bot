#!/usr/bin/env python3
"""Deterministic local comparison for scoped RAG retrieval.

This is a synthetic SQLite benchmark. It does not call Discord, Ollama, or an
AI provider. The legacy path models the previous query shape for global LTM:
channel/guild filters admitted every blank-scope LTM row, followed by a Python
loop that decoded and normalized every candidate vector.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from rag_memory import (  # noqa: E402
    EMBED_DIM,
    MemoryRequester,
    RAGMemoryManager,
    _embedding_to_blob,
    _blob_to_embedding,
)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    at = (len(ordered) - 1) * percentile
    lo = int(at)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo)


def _insert_ltm(mgr: RAGMemoryManager, *, row_id: str, guild: str, content: str, vector: bytes, scope: str, metadata: str, channel_id: str = "") -> None:
    mgr._db.execute(
        "INSERT INTO vectors (id, kind, channel_id, guild_id, author, author_id, "
        "source, content, content_hash, embedding, metadata, scope, importance, "
        "parent_id, chunk_index, downvotes, timestamp, created_at) "
        "VALUES (?, 'ltm', ?, ?, 'synthetic', 'synthetic', 'user', ?, ?, ?, ?, ?, "
        "5, '', 0, 0, ?, ?)",
        (
            row_id, channel_id, guild, content, row_id, vector, metadata, scope,
            "2026-01-01T00:00:00+00:00", time.time(),
        ),
    )


def _seed(mgr: RAGMemoryManager, guild_count: int, facts_per_guild: int) -> tuple[MemoryRequester, np.ndarray]:
    query = np.zeros(EMBED_DIM, dtype=np.float32)
    query[0] = 1.0
    blob = _embedding_to_blob(query)
    target_guild = f"guild-{guild_count - 1}"
    target_channel = f"channel-{guild_count - 1}-0"
    requester = MemoryRequester(
        user_id="benchmark-user",
        channel_id=target_channel,
        guild_id=target_guild,
        channel_is_public=False,
    )
    for guild_index in range(guild_count):
        for fact_index in range(facts_per_guild):
            row_id = f"legacy-{guild_index}-{fact_index}"
            _insert_ltm(
                mgr,
                row_id=row_id,
                guild="",
                channel_id="",
                content=f"legacy shared fact {guild_index} {fact_index}",
                vector=blob,
                scope="global",
                metadata='{"source_kind":"legacy_unscoped"}',
            )
    for fact_index in range(facts_per_guild):
        row_id = f"scoped-{guild_count}-{fact_index}"
        metadata = json.dumps({
            "source_user_id": "benchmark-user",
            "source_channel_id": target_channel,
            "source_guild_id": target_guild,
            "source_is_dm": False,
            "source_channel_public": False,
            "source_kind": "scoped_ltm",
            "visibility": "private",
        })
        _insert_ltm(
            mgr,
            row_id=row_id,
            guild=target_guild,
            channel_id=target_channel,
            content=f"target scoped fact {fact_index}",
            vector=blob,
            scope=f"channel:{target_channel}",
            metadata=metadata,
        )
    return requester, query


def _legacy_search(mgr: RAGMemoryManager, requester: MemoryRequester, query: np.ndarray) -> int:
    rows = mgr._db.execute(
        "SELECT embedding FROM vectors WHERE kind='ltm' AND embedding IS NOT NULL "
        "AND (channel_id=? OR channel_id='') AND (guild_id=? OR guild_id='')",
        (requester.channel_id, requester.guild_id),
    ).fetchall()
    query_norm = query / (np.linalg.norm(query) + 1e-8)
    for row in rows:
        vector = _blob_to_embedding(row["embedding"])
        if len(vector) == EMBED_DIM:
            vector = vector / (np.linalg.norm(vector) + 1e-8)
            float(np.dot(query_norm, vector))
    return len(rows)


def _rss_bytes() -> int:
    """Current process RSS on Linux; zero when procfs is unavailable."""
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * int(__import__("os").sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError, AttributeError):
        return 0


def _sqlite_file_sizes(db_path: Path) -> dict[str, int]:
    sizes = {}
    for suffix, name in (("", "main"), ("-wal", "wal"), ("-shm", "shm")):
        try:
            sizes[name] = Path(f"{db_path}{suffix}").stat().st_size
        except OSError:
            sizes[name] = 0
    return sizes


async def _measure(
    mgr: RAGMemoryManager,
    requester: MemoryRequester,
    query: np.ndarray,
    iterations: int,
) -> dict:
    async def query_embedding(_text: str):
        return query

    mgr._embed_for_query = query_embedding
    wall_started = time.perf_counter()
    cpu_started = time.process_time()

    async def measure_loop_lag(operation) -> float:
        loop = asyncio.get_running_loop()
        lag: list[float] = []
        finished = False
        expected = [loop.time() + 0.005]

        def heartbeat():
            now = loop.time()
            lag.append(max(0.0, now - expected[0]) * 1000.0)
            if not finished:
                expected[0] = max(expected[0] + 0.005, now + 0.005)
                loop.call_at(expected[0], heartbeat)

        loop.call_at(expected[0], heartbeat)
        await operation()
        finished = True
        await asyncio.sleep(0.006)
        return max(lag, default=0.0)

    legacy_times = []
    legacy_candidates = 0

    async def legacy_phase():
        nonlocal legacy_candidates
        for _ in range(iterations):
            started = time.perf_counter()
            legacy_candidates = _legacy_search(mgr, requester, query)
            legacy_times.append((time.perf_counter() - started) * 1000.0)
            await asyncio.sleep(0)

    legacy_loop_lag = await measure_loop_lag(legacy_phase)

    current_times = []
    current_results = 0

    async def current_phase():
        nonlocal current_results
        for _ in range(iterations):
            started = time.perf_counter()
            rows = await mgr.rag_search(
                "benchmark query",
                kinds=["ltm"],
                requester=requester,
                top_k=8,
                min_similarity=0.1,
                apply_recency=False,
                exclude_negatives=False,
            )
            current_results = len(rows)
            current_times.append((time.perf_counter() - started) * 1000.0)
            await asyncio.sleep(0)

    scoped_loop_lag = await measure_loop_lag(current_phase)
    wall_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    return {
        "iterations": iterations,
        "legacy_candidates_per_query": legacy_candidates,
        "scoped_results_per_query": current_results,
        "legacy_ms": {
            "p50": round(_percentile(legacy_times, 0.50), 3),
            "p95": round(_percentile(legacy_times, 0.95), 3),
            "p99": round(_percentile(legacy_times, 0.99), 3),
            "throughput_per_second": round(1000.0 / statistics.mean(legacy_times), 2),
        },
        "scoped_ms": {
            "p50": round(_percentile(current_times, 0.50), 3),
            "p95": round(_percentile(current_times, 0.95), 3),
            "p99": round(_percentile(current_times, 0.99), 3),
            "throughput_per_second": round(1000.0 / statistics.mean(current_times), 2),
        },
        "process_cpu_seconds": round(cpu_seconds, 4),
        "process_cpu_percent_of_one_core": round(
            100.0 * cpu_seconds / max(wall_seconds, 1e-9), 1
        ),
        "legacy_event_loop_lag_ms_max": round(legacy_loop_lag, 3),
        "scoped_event_loop_lag_ms_max": round(scoped_loop_lag, 3),
        "event_loop_lag_ms_max": round(max(legacy_loop_lag, scoped_loop_lag), 3),
    }


async def _run(args) -> dict:
    results = []
    for guild_count in args.guilds:
        with tempfile.TemporaryDirectory(prefix="maxwell-rag-bench-") as temp_dir:
            mgr = RAGMemoryManager(temp_dir)
            def skip_embedding(coro):
                coro.close()
                return
            mgr._spawn = skip_embedding
            requester, query = _seed(mgr, guild_count, args.facts_per_guild)
            rss_before = _rss_bytes()
            tracemalloc.start()
            result = await _measure(mgr, requester, query, args.iterations)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            rss_after = _rss_bytes()
            sqlite_sizes = _sqlite_file_sizes(Path(mgr.db_path))
            result.update({
                "guilds": guild_count,
                "facts_per_guild": args.facts_per_guild,
                "database_bytes": sum(sqlite_sizes.values()),
                "database_main_file_bytes": sqlite_sizes["main"],
                "database_wal_bytes": sqlite_sizes["wal"],
                "database_shm_bytes": sqlite_sizes["shm"],
                "process_rss_before_bytes": rss_before,
                "process_rss_after_bytes": rss_after,
                "process_rss_delta_bytes": rss_after - rss_before,
                "python_tracemalloc_peak_bytes": peak,
                "sqlite_version": mgr._db.execute("select sqlite_version()").fetchone()[0],
            })
            results.append(result)
            mgr._db.close()
    return {
        "benchmark": "synthetic RAG scope and vector scoring",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": np.__version__,
        "provider_calls": 0,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guilds", type=int, nargs="+", default=[100, 500, 1000])
    parser.add_argument("--facts-per-guild", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=25)
    args = parser.parse_args()
    if any(count < 1 for count in args.guilds):
        parser.error("guild counts must be positive")
    if args.facts_per_guild < 1 or args.iterations < 1:
        parser.error("facts-per-guild and iterations must be positive")
    print(json.dumps(asyncio.run(_run(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
