"""Subprocess lifecycle helpers for cancellable media work."""

import asyncio
import contextlib


async def communicate_process(
    process: asyncio.subprocess.Process, *, timeout: float
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        # Drain pipes as well as reaping the child: wait() alone can hang when
        # cancelled communicate() left a full pipe with its reader paused.
        await process.communicate()
        await process.wait()
        raise
