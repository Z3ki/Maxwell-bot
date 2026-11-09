"""One-way allowlisted chat controls. No environment/DB/history/secret export."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from standby import policy as standby_policy
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
    if text != str(int(text)) or int(text) <= 0:
        raise PolicyDenied("Discord IDs must be positive canonical decimal strings")
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


def effective_controls(
    source: dict[str, Any], *, blacklist: list[Any]
) -> dict[str, Any]:
    """Resolve primary overrides without loading its runtime or exporting other state."""
    # Export runs on the primary checkout. Standby's isolated import/runtime paths
    # must not need primary modules, including the shared defaults.
    from control_defaults import DEFAULT_CONTROL, parse_bool

    if not isinstance(source, dict):
        raise PolicyDenied("Primary control settings must be an object")
    if not isinstance(blacklist, list) or len(blacklist) > 10000:
        raise PolicyDenied("Authoritative blacklist must be an ID list")
    blocked_users = {snowflake(item) for item in blacklist}
    effective = {key: source.get(key, DEFAULT_CONTROL[key]) for key in KEYS}
    # The primary ignores persisted persona overrides: this preference is code-owned.
    effective["base_personality"] = DEFAULT_CONTROL["base_personality"]
    for key in BOOLS:
        value = effective[key]
        if (
            not isinstance(value, (bool, int, str))
            or str(value).strip().lower()
            not in {"true", "false", "1", "0", "yes", "no", "on", "off"}
        ):
            raise PolicyDenied("Invalid primary boolean control")
        effective[key] = parse_bool(value, DEFAULT_CONTROL[key])
    controls = controls_only(effective)
    controls["ignore_users"] = sorted(set(controls["ignore_users"]) | blocked_users)
    # The union must satisfy the same bounds as an imported snapshot.
    return controls_only(controls, strict=True)


def export_snapshot(
    source: dict[str, Any],
    *,
    blacklist: list[Any],
    revision: str,
    now: float | None = None,
) -> dict[str, Any]:
    controls = effective_controls(source, blacklist=blacklist)
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
        type(data["schema"]) is not int
        or data["schema"] != 1
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


class ConfigDurabilityError(OSError):
    """A validated snapshot was replaced but its durable commit is uncertain."""


def atomic_import(
    data: dict[str, Any], target: Path, *, now: float | None = None
) -> None:
    data = validate_snapshot(data, now=now)
    lock = os.open(
        target.with_name(target.name + ".lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        # Compare and replace share one cross-process lock for CLI and receivers.
        # Nonblocking acquisition bounds request time; the next refresh may retry.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise PolicyDenied("Refusing to replace a non-regular config path")
        if target.exists():
            previous = validate_snapshot(read_json(target), now=now)
            if data["exported_at"] < previous["exported_at"]:
                raise PolicyDenied("Refusing older configuration")
            if (
                data["exported_at"] == previous["exported_at"]
                and data["sha256"] != previous["sha256"]
            ):
                raise PolicyDenied("Conflicting configuration version")
        # Open the parent before replacement, so an open failure is pre-commit.
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        name = None
        published = False
        try:
            fd, name = tempfile.mkstemp(prefix=".chat-config-", dir=target.parent)
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
            published = True
            os.fsync(directory)
        except Exception as error:
            if published:
                raise ConfigDurabilityError(
                    "Validated snapshot replaced; durability unconfirmed"
                ) from error
            raise
        finally:
            with contextlib.suppress(OSError):
                os.close(directory)
            if name is not None:
                with contextlib.suppress(FileNotFoundError):
                    Path(name).unlink()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def chat_allowed(
    controls: dict[str, Any], *, user_id: int, channel_id: int, guild_id: int | None
) -> bool:
    """Deny unless every restriction field is present and well typed.

    Missing or mistyped ignore/block lists must not become an empty allow.
    """
    try:
        normalized = controls_only(controls, strict=True)
    except PolicyDenied:
        return False
    user, channel = str(user_id), str(channel_id)
    if not normalized["bot_enabled"] or not normalized["reply_mentions"]:
        return False
    if user in normalized["ignore_users"] or channel in normalized["blocked_channels"]:
        return False
    allowed = normalized["allowed_channels"]
    if allowed and channel not in allowed:
        return False
    solo = normalized["guild_solo_channel"].get(str(guild_id))
    return not solo or channel == solo


def read_json(path: Path) -> Any:
    """Reject ambiguous authoritative JSON rather than discarding restrictions."""
    limit = standby_policy.MAX_AUTHORITATIVE_JSON
    try:
        info = path.lstat()
    except OSError as error:
        raise PolicyDenied("Authoritative input is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PolicyDenied("Authoritative input must be a regular non-symlink file")
    if info.st_size > limit:
        raise PolicyDenied("Authoritative input is too large")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise PolicyDenied("Authoritative input is unavailable") from error
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_size > limit:
            raise PolicyDenied("Authoritative input must be a bounded regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                text = stream.read(limit + 1)
            except UnicodeDecodeError as error:
                raise PolicyDenied("Authoritative input is not UTF-8 text") from error
    finally:
        if fd >= 0:
            os.close(fd)
    return standby_policy.parse_json(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "import", "status"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--blacklist",
        type=Path,
        help="Authoritative primary blacklist JSON (default: blacklist.json beside --input)",
    )
    parser.add_argument("--revision", default="manual")
    args = parser.parse_args()
    data = read_json(args.input)
    if args.action == "status":
        data = validate_snapshot(data)
        print(json.dumps({k: data[k] for k in ("revision", "exported_at", "sha256")}))
    elif args.action == "export":
        if args.output is None:
            parser.error("--output required")
        blacklist = read_json(args.blacklist or args.input.with_name("blacklist.json"))
        atomic_import(
            export_snapshot(data, blacklist=blacklist, revision=args.revision),
            args.output,
        )
    else:
        if args.output is None:
            parser.error("--output required")
        atomic_import(data, args.output)


if __name__ == "__main__":
    main()
