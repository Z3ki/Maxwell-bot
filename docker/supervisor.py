"""Keep the bot and optional API alive as one container service."""

import asyncio
import os
import signal
import sys
from pathlib import Path


async def supervise(commands: list[list[str]], stop_timeout: float = 15.0) -> int:
    """Stop all children if any child exits, or the container receives a signal.

    An API crash must restart the container rather than leave a healthy-looking
    bot with a dead dashboard. Forward shutdown and reap every child.
    """
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    processes = []
    waiters = []
    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        loop.add_signal_handler(sig, stopped.set)
    try:
        for command in commands:
            # Keep each child available for cleanup if the next spawn fails.
            processes.append(await asyncio.create_subprocess_exec(*command))  # noqa: PERF401
        waiters = [asyncio.create_task(proc.wait()) for proc in processes]
        stop_waiter = asyncio.create_task(stopped.wait())
        waiters.append(stop_waiter)
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if stop_waiter in done:
            return 0
        for command, proc in zip(commands, processes):
            if proc.returncode is not None:
                print(
                    f"Service {Path(command[1]).name} exited ({proc.returncode}); stopping container",
                    file=sys.stderr,
                )
        return 1
    finally:
        for proc in processes:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        if processes:
            _, pending = await asyncio.wait(
                [asyncio.create_task(proc.wait()) for proc in processes],
                timeout=stop_timeout,
            )
            if pending:
                for proc in processes:
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                await asyncio.gather(*pending)
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        for sig in signals:
            loop.remove_signal_handler(sig)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    commands = [[sys.executable, str(root / "bot.py"), *sys.argv[1:]]]
    if os.getenv("MAXWELL_START_API", "1") != "0":
        commands.append([sys.executable, str(root / "api/api_server.py")])
    return asyncio.run(supervise(commands))


if __name__ == "__main__":
    raise SystemExit(main())
