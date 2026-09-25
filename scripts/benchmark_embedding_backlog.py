#!/usr/bin/env python3
"""Benchmark the indexed SQLite scan used to recover pending embeddings.

This synthetic microbenchmark makes no model/provider calls. It compares the
pending-row scan before and after the partial NULL-vector index on identical
temporary data, with pending rows placed after a large embedded corpus.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path


QUERY = (
    "SELECT rowid, id FROM vectors "
    "WHERE embedding IS NULL AND rowid > ? ORDER BY rowid LIMIT ?"
)


def _drain_pending(db: sqlite3.Connection, batch_size: int) -> int:
    last_rowid = 0
    count = 0
    while True:
        rows = db.execute(QUERY, (last_rowid, batch_size)).fetchall()
        if not rows:
            return count
        last_rowid = rows[-1][0]
        count += len(rows)


def _percentile_ms(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index] * 1000.0, 3)


def _measure(db: sqlite3.Connection, iterations: int, batch_size: int) -> list[float]:
    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        _drain_pending(db, batch_size)
        timings.append(time.perf_counter() - started)
    return timings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--pending", type=int, default=1_000)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=4)
    args = parser.parse_args()
    if args.rows < 1 or not 0 <= args.pending <= args.rows:
        parser.error("require rows >= 1 and 0 <= pending <= rows")
    if args.iterations < 1 or args.batch_size < 1:
        parser.error("iterations and batch-size must be positive")

    with tempfile.TemporaryDirectory(prefix="maxwell-embed-backlog-") as directory:
        db = sqlite3.connect(str(Path(directory) / "benchmark.sqlite"))
        db.execute(
            "CREATE TABLE vectors (id TEXT PRIMARY KEY, embedding BLOB, content TEXT)"
        )
        start_pending = args.rows - args.pending
        db.executemany(
            "INSERT INTO vectors (id, embedding, content) VALUES (?, ?, ?)",
            (
                (
                    str(row_id),
                    None if row_id > start_pending else b"vector",
                    "synthetic",
                )
                for row_id in range(1, args.rows + 1)
            ),
        )
        db.commit()

        baseline_plan = db.execute(
            "EXPLAIN QUERY PLAN " + QUERY, (0, args.batch_size)
        ).fetchall()
        baseline_count = _drain_pending(db, args.batch_size)
        if baseline_count != args.pending:
            raise RuntimeError(
                f"expected {args.pending} pending rows, found {baseline_count}"
            )
        baseline = _measure(db, args.iterations, args.batch_size)

        db.execute(
            "CREATE INDEX idx_vectors_pending_embedding "
            "ON vectors(embedding) WHERE embedding IS NULL"
        )
        db.commit()
        indexed_plan = db.execute(
            "EXPLAIN QUERY PLAN " + QUERY, (0, args.batch_size)
        ).fetchall()
        indexed_count = _drain_pending(db, args.batch_size)
        if indexed_count != args.pending:
            raise RuntimeError(
                f"indexed scan found {indexed_count}, expected {args.pending}"
            )
        indexed = _measure(db, args.iterations, args.batch_size)

        print(
            f"SQLite {sqlite3.sqlite_version}; {args.rows:,} rows; "
            f"{args.pending:,} pending; {args.iterations} iterations"
        )
        print(
            "reference(no index) p50/p95/p99: "
            f"{_percentile_ms(baseline, .50):.3f}/"
            f"{_percentile_ms(baseline, .95):.3f}/"
            f"{_percentile_ms(baseline, .99):.3f} ms"
        )
        print(
            "partial index p50/p95/p99: "
            f"{_percentile_ms(indexed, .50):.3f}/"
            f"{_percentile_ms(indexed, .95):.3f}/"
            f"{_percentile_ms(indexed, .99):.3f} ms"
        )
        ref_p50 = statistics.median(baseline) * 1000.0
        new_p50 = statistics.median(indexed) * 1000.0
        print(f"p50 speedup: {ref_p50 / new_p50:.2f}x" if new_p50 else "p50 speedup: n/a")
        print("reference query plan:", " | ".join(row[3] for row in baseline_plan))
        print("indexed query plan:", " | ".join(row[3] for row in indexed_plan))
        db.close()


if __name__ == "__main__":
    main()
