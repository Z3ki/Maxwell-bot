#!/usr/bin/env python3
"""Safely update KEY=VALUE entries in Maxwell dotenv files."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _format_value(value: str) -> str:
    """Format value for .env output.

    Quotes values that contain spaces, leading/trailing whitespace, quotes, backslashes,
    or '#' comment indicators so python-dotenv / sh parsers do not truncate or corrupt them.
    """
    needs_quotes = (
        not value
        or value.startswith(" ")
        or value.endswith(" ")
        or "\t" in value
        or " #" in value
        or value.startswith("#")
        or '"' in value
        or "'" in value
        or "\\" in value
    )
    if needs_quotes:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def set_env(path: str | Path, key: str, value: str) -> None:
    """Set *key* to *value* in *path*, preserving unrelated lines.

    Values are safely escaped and quoted when necessary.
    Newlines are rejected because dotenv entries are one logical line in this project.
    """

    if not _KEY_RE.match(key):
        raise ValueError(f"invalid environment key: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"environment value for {key} must be one line")

    env_path = Path(path)
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    formatted_val = _format_value(value)
    line = f"{key}={formatted_val}"
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}=.*$", re.MULTILINE)
    # Use lambda replacement to prevent \1, \g<name> backreference expansion errors
    new_text, count = pattern.subn(lambda m: line, text, count=1)
    if count == 0:
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += line + "\n"
    env_path.write_text(new_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set one key in a dotenv file.")
    parser.add_argument("path", help="dotenv file to update")
    parser.add_argument("key", help="environment variable name")
    parser.add_argument("value", nargs="?", default=None, help="value to write (or pass via SET_ENV_VALUE env var)")
    args = parser.parse_args()
    val = args.value
    if val is None:
        val = os.environ.get("SET_ENV_VALUE", "")
    set_env(args.path, args.key, val)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
