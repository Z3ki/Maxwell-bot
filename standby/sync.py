"""One-way allowlisted chat controls. No environment/DB/history/secret export."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from standby.policy import PolicyDenied

BOOLS = frozenset({"bot_enabled", "reply_mentions"})
LISTS = frozenset({"ignore_users", "allowed_channels", "blocked_channels"})
KEYS = BOOLS | LISTS | {"guild_solo_channel", "base_personality"}


def snowflake(value: Any) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise PolicyDenied("Invalid Discord ID")
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not 1 <= len(text) <= 22:
        raise PolicyDenied("Invalid Discord ID")
    return text


def controls_only(source: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    if not isinstance(source, dict) or (strict and set(source) - KEYS):
        raise PolicyDenied("Unsupported snapshot control")
    if KEYS - source.keys():
        raise PolicyDenied(
            "Complete effective chat controls required; refusing partial export"
        )
    result: dict[str, Any] = {}
    for key in BOOLS & source.keys():
        if type(source[key]) is not bool:
            raise PolicyDenied("Expected boolean")
        result[key] = source[key]
    for key in LISTS & source.keys():
        rows = source[key]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise PolicyDenied("Invalid ID list")
        result[key] = sorted({snowflake(item) for item in rows})
    if "guild_solo_channel" in source:
        rows = source["guild_solo_channel"]
        if not isinstance(rows, dict) or len(rows) > 10000:
            raise PolicyDenied("Invalid guild mapping")
        result["guild_solo_channel"] = {
            snowflake(k): snowflake(v) for k, v in rows.items()
        }
    if "base_personality" in source:
        value = source["base_personality"]
        if not isinstance(value, str) or len(value) > 2000:
            raise PolicyDenied("Invalid style preference")
        result["base_personality"] = value
    return result


def digest(controls: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(controls, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def export_snapshot(
    source: dict[str, Any], *, revision: str, now: float | None = None
) -> dict[str, Any]:
    controls = controls_only(source)
    return {
        "schema": 1,
        "revision": revision,
        "exported_at": time.time() if now is None else now,
        "sha256": digest(controls),
        "controls": controls,
    }


def validate_snapshot(
    data: dict[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {
        "schema",
        "revision",
        "exported_at",
        "sha256",
        "controls",
    }:
        raise PolicyDenied("Unsupported snapshot envelope")
    if (
        data["schema"] != 1
        or not isinstance(data["revision"], str)
        or len(data["revision"]) > 100
    ):
        raise PolicyDenied("Unsupported snapshot revision")
    stamp = data["exported_at"]
    current = time.time() if now is None else now
    if type(stamp) not in {int, float} or not 0 < stamp <= current + 300:
        raise PolicyDenied("Invalid snapshot timestamp")
    controls = controls_only(data["controls"], strict=True)
    if digest(controls) != data["sha256"]:
        raise PolicyDenied("Snapshot checksum mismatch")
    return {**data, "controls": controls}


def atomic_import(
    data: dict[str, Any], target: Path, *, now: float | None = None
) -> None:
    data = validate_snapshot(data, now=now)
    if target.exists():
        previous = validate_snapshot(json.loads(target.read_text()), now=now)
        if data["exported_at"] < previous["exported_at"]:
            raise PolicyDenied("Refusing older configuration")
        if (
            data["exported_at"] == previous["exported_at"]
            and data["sha256"] != previous["sha256"]
        ):
            raise PolicyDenied("Conflicting configuration version")
    fd, name = tempfile.mkstemp(prefix=".chat-config-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def chat_allowed(
    controls: dict[str, Any], *, user_id: int, channel_id: int, guild_id: int | None
) -> bool:
    user, channel = str(user_id), str(channel_id)
    if not controls.get("bot_enabled", True) or not controls.get(
        "reply_mentions", True
    ):
        return False
    if user in controls.get("ignore_users", []) or channel in controls.get(
        "blocked_channels", []
    ):
        return False
    allowed = controls.get("allowed_channels", [])
    if allowed and channel not in allowed:
        return False
    solo = controls.get("guild_solo_channel", {}).get(str(guild_id))
    return not solo or channel == solo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "import", "status"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revision", default="manual")
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    if args.action == "status":
        data = validate_snapshot(data)
        print(json.dumps({k: data[k] for k in ("revision", "exported_at", "sha256")}))
    elif args.action == "export":
        if args.output is None:
            parser.error("--output required")
        atomic_import(export_snapshot(data, revision=args.revision), args.output)
    else:
        if args.output is None:
            parser.error("--output required")
        atomic_import(data, args.output)


if __name__ == "__main__":
    main()
