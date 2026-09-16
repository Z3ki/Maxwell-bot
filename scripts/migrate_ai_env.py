#!/usr/bin/env python3
"""Migrate Maxwell's primary provider variables to provider-neutral AI_* names.

The runtime still understands the historical OLLAMA_* keys. This script keeps
those keys as interpolation aliases so old code and tooling continue to work,
while humans only need to edit AI_API_URL / AI_MODEL / AI_API_KEY.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

PAIRS = (
    ("OLLAMA_BASE_URL", "AI_API_URL"),
    ("OLLAMA_MODEL", "AI_MODEL"),
    ("OLLAMA_API_KEY", "AI_API_KEY"),
)
ASSIGN_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")


def migrate(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    raw: dict[str, str] = {}
    first_legacy_index: int | None = None

    legacy_keys = {old for old, _ in PAIRS}
    friendly_keys = {new for _, new in PAIRS}

    for i, line in enumerate(lines):
        match = ASSIGN_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key in legacy_keys | friendly_keys:
            raw[key] = value
        if first_legacy_index is None and key in legacy_keys:
            first_legacy_index = i

    if all(
        new in raw and raw.get(old, "").strip() == f"${{{new}}}"
        for old, new in PAIRS
    ):
        return False

    values: dict[str, str] = {}
    for old, new in PAIRS:
        candidate = raw.get(new)
        if candidate is None or candidate.strip() == "":
            candidate = raw.get(old, "")
        if candidate.strip() == f"${{{new}}}":
            candidate = ""
        values[new] = candidate

    output: list[str] = []
    inserted = False

    def add_block() -> None:
        nonlocal inserted
        if inserted:
            return
        if output and output[-1].strip():
            output.append("")
        output.extend(
            [
                "# Primary AI provider (provider-neutral names; edit these)",
                f"AI_API_URL={values['AI_API_URL']}",
                f"AI_MODEL={values['AI_MODEL']}",
                f"AI_API_KEY={values['AI_API_KEY']}",
                "# Compatibility aliases for older Maxwell code/config tooling.",
                "OLLAMA_BASE_URL=${AI_API_URL}",
                "OLLAMA_MODEL=${AI_MODEL}",
                "OLLAMA_API_KEY=${AI_API_KEY}",
            ]
        )
        inserted = True

    for i, line in enumerate(lines):
        match = ASSIGN_RE.match(line)
        key = match.group(1) if match else None

        if i == first_legacy_index:
            add_block()

        if key in friendly_keys or key in legacy_keys:
            continue
        output.append(line)

    if not inserted:
        add_block()

    new_text = "\n".join(output).rstrip() + "\n"
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rename Maxwell provider env vars to provider-neutral AI_* names"
    )
    parser.add_argument("path", nargs="?", default=".env")
    args = parser.parse_args()
    path = Path(args.path)
    if not path.exists():
        parser.error(f"{path} does not exist")
    changed = migrate(path)
    print(f"{'updated' if changed else 'already migrated'}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
