#!/usr/bin/env python3
"""Safely update KEY=VALUE entries in Maxwell dotenv files."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def set_env(path: str | Path, key: str, value: str) -> None:
    """Set *key* to *value* in *path*, preserving unrelated lines.

    Values are written literally after the first ``=`` so URLs, ampersands,
    additional equals signs, colons, and spaces do not need shell escaping or
    sed-compatible escaping. Newlines are rejected because dotenv entries are
    one logical line in this project.
    """

    if not _KEY_RE.match(key):
        raise ValueError(f"invalid environment key: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"environment value for {key} must be one line")

    env_path = Path(path)
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"{key}={value}"
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}=.*$", re.MULTILINE)
    new_text, count = pattern.subn(line, text, count=1)
    if count == 0:
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += line + "\n"
    env_path.write_text(new_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set one key in a dotenv file.")
    parser.add_argument("path", help="dotenv file to update")
    parser.add_argument("key", help="environment variable name")
    parser.add_argument("value", help="value to write")
    args = parser.parse_args()
    set_env(args.path, args.key, args.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
