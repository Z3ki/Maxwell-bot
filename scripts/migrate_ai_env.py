#!/usr/bin/env python3
"""Migrate Maxwell's provider variables to provider-neutral AI_* names.

The runtime still understands the historical OLLAMA_* keys. This script keeps
those keys as interpolation aliases so old code and tooling continue to work,
while humans edit provider-neutral AI_* names.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PAIRS = (
    ("OLLAMA_BASE_URL", "AI_BASE_URL"),
    ("OLLAMA_MODEL", "AI_MODEL"),
    ("OLLAMA_API_KEY", "AI_API_KEY"),
    ("OLLAMA_TEMPERATURE", "AI_TEMPERATURE"),
    ("OLLAMA_DISABLE_REASONING", "AI_DISABLE_REASONING"),
    ("OLLAMA_REASONING_EFFORT", "AI_REASONING_EFFORT"),
    ("OLLAMA_FALLBACK_BASE_URL", "AI_FALLBACK_BASE_URL"),
    ("OLLAMA_FALLBACK_API_KEY", "AI_FALLBACK_API_KEY"),
    ("OLLAMA_FALLBACK_MODEL", "AI_FALLBACK_MODEL"),
    ("OLLAMA_FALLBACK_DISABLE_REASONING", "AI_FALLBACK_DISABLE_REASONING"),
    ("OLLAMA_FALLBACK_REASONING_EFFORT", "AI_FALLBACK_REASONING_EFFORT"),
    ("OLLAMA_VISION_BASE_URL", "AI_VISION_BASE_URL"),
    ("OLLAMA_VISION_API_KEY", "AI_VISION_API_KEY"),
    ("OLLAMA_VISION_MODEL", "AI_VISION_MODEL"),
    ("OLLAMA_VISION_DISABLE_REASONING", "AI_VISION_DISABLE_REASONING"),
    ("OLLAMA_RETRY_ATTEMPTS", "AI_RETRY_ATTEMPTS"),
    ("OLLAMA_EMPTY_RESPONSE_RETRIES", "AI_EMPTY_RESPONSE_RETRIES"),
    ("OLLAMA_ENDPOINT_COOLDOWN_SECONDS", "AI_ENDPOINT_COOLDOWN_SECONDS"),
    ("OLLAMA_OPENCODE_SESSION", "AI_OPENCODE_SESSION"),
    ("OLLAMA_MAX_TOKENS", "AI_MAX_OUTPUT_TOKENS"),
)
ASSIGN_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")


def migrate(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    raw: dict[str, str] = {}
    first_legacy_index: int | None = None

    legacy_keys = {old for old, _ in PAIRS} | {"AI_API_URL"}
    friendly_keys = {new for _, new in PAIRS}

    for i, line in enumerate(lines):
        match = ASSIGN_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key in legacy_keys | friendly_keys:
            raw[key] = value
        if first_legacy_index is None and key in legacy_keys | friendly_keys:
            first_legacy_index = i

    active_pairs = [
        (old, new)
        for old, new in PAIRS
        if old in raw or new in raw or (new == "AI_BASE_URL" and "AI_API_URL" in raw)
    ]
    if all(
        new in raw and raw.get(old, "").strip() == f"${{{new}}}"
        for old, new in active_pairs
    ) and ("AI_API_URL" not in raw or raw["AI_API_URL"].strip() == "${AI_BASE_URL}"):
        return False

    values: dict[str, str] = {}
    for old, new in active_pairs:
        candidate = raw.get(new)
        if new == "AI_BASE_URL" and (
            candidate is None or candidate.strip() in {"", "${AI_API_URL}"}
        ):
            candidate = raw.get("AI_API_URL")
        # Presence wins for credentials/optional routes, including empty values.
        if candidate is None or (
            new in {"AI_BASE_URL", "AI_MODEL"} and not candidate.strip()
        ):
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
        output.append("# AI provider (provider-neutral names; edit these)")
        output.extend(f"{new}={values[new]}" for _, new in active_pairs)
        output.append("# Deprecated compatibility aliases for older tooling.")
        if "AI_BASE_URL" in values:
            output.append("AI_API_URL=${AI_BASE_URL}")
        output.extend(f"{old}=${{{new}}}" for old, new in active_pairs)
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
