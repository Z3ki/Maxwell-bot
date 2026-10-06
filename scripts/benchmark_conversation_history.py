#!/usr/bin/env python3
"""Measure local scoped-history storage; not Discord or provider capacity."""

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conversation_memory import ConversationMemoryManager  # noqa: E402
from maxwell_core.memory.scope import MemoryRequester  # noqa: E402


async def sample(guilds, iterations):
    with tempfile.TemporaryDirectory() as directory:
        memory = ConversationMemoryManager(directory, max_messages=20)
        try:
            start = time.perf_counter()
            for guild in range(guilds):
                for number in range(5):
                    await memory.add_to_channel_memory(str(guild), {
                        "message_id": f"{guild}-{number}", "guild_id": str(guild),
                        "author_id": "user", "content": "synthetic recent history",
                    })
            write_seconds = time.perf_counter() - start
            reads = []
            for number in range(iterations * guilds):
                guild = str(number % guilds)
                requester = MemoryRequester("user", guild, guild)
                start = time.perf_counter()
                rows = await memory.get_channel_memory(guild, requester=requester)
                reads.append((time.perf_counter() - start) * 1000)
                assert len(rows) == 5
                assert all(row["message_id"].startswith(guild + "-") for row in rows)
            reads.sort()
            return {
                "guilds": guilds, "rows": guilds * 5,
                "write_seconds": round(write_seconds, 3),
                "read_p50_ms": round(statistics.median(reads), 3),
                "read_p95_ms": round(reads[max(0, int(len(reads) * .95) - 1)], 3),
                "embedding_requests": 0,
            }
        finally:
            await memory.flush()


async def run(args):
    results = [await sample(count, args.iterations) for count in args.guilds]
    print(json.dumps({"notes": __doc__, "results": results}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guilds", nargs="+", type=int, default=[100, 500, 1000])
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args()
    if args.iterations < 1 or any(count < 1 for count in args.guilds):
        parser.error("guild counts and iterations must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
