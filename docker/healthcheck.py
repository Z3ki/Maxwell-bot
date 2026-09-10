#!/usr/bin/env python3
"""Check for the bot process without mistaking this probe for the bot."""

import sys
from pathlib import Path


def bot_is_running(proc_root: Path = Path("/proc")) -> bool:
    for cmdline in proc_root.glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().split(b"\0")
        except OSError:
            # A process may exit between listing /proc and reading its cmdline.
            continue
        if len(args) > 1 and Path(args[0].decode(errors="replace")).name.startswith(
            "python"
        ):
            if args[1] in (b"bot.py", b"/app/bot.py"):
                return True
    return False


if __name__ == "__main__":
    sys.exit(0 if bot_is_running() else 1)
