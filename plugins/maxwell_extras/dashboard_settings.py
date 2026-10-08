"""Small, scoped storage operations shared by web and Discord settings."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

from control_defaults import GUILD_CAPABILITIES
from utils import FileLock, _atomic_json_write_sync


def read_json(path: Path, kind, default):
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Settings could not be read safely. Contact the operator."
        ) from exc
    if not isinstance(value, kind):
        raise ValueError("Settings could not be read safely. Contact the operator.")  # noqa: TRY004 - callers fail closed on ValueError
    return value


def server_settings(data_dir: Path, guild_id: str) -> dict:
    control = read_json(data_dir / "bot_control.json", dict, {})
    disabled = control.get("guild_disabled_capabilities", {}).get(guild_id, [])
    on = read_json(data_dir / "progress_servers.json", list, [])
    off = read_json(data_dir / "progress_servers_off.json", list, [])
    tickets = read_json(data_dir / "ticket_greeting_servers.json", list, [])
    return {
        "channel": control.get("guild_solo_channel", {}).get(guild_id, ""),
        "capabilities": [key for key in GUILD_CAPABILITIES if key not in disabled],
        "progress": "on" if guild_id in on else "off" if guild_id in off else "default",
        "ticket": guild_id in tickets,
        "plugins": control.get("guild_plugin_overrides", {}).get(guild_id, {}),
    }


def save_server_settings(
    data_dir: Path, guild_id: str, changes: dict, *, fallback: dict | None = None
) -> dict:
    """Merge only this guild; preserve other guilds and operator controls."""
    if not guild_id.isdigit() or not changes.keys() <= {
        "channel",
        "capabilities",
        "progress",
        "ticket",
        "plugins",
    }:
        raise ValueError("Unsupported server setting.")
    if "channel" in changes and (
        not isinstance(changes["channel"], str)
        or changes["channel"]
        and not changes["channel"].isdigit()
    ):
        raise ValueError("Choose a response channel.")
    if "capabilities" in changes and (
        not isinstance(changes["capabilities"], list)
        or not all(
            isinstance(key, str) and key in GUILD_CAPABILITIES
            for key in changes["capabilities"]
        )
    ):
        raise ValueError("Choose supported tool groups.")
    if "plugins" in changes and (
        not isinstance(changes["plugins"], dict)
        or not all(
            key.isidentifier() and type(value) is bool
            for key, value in changes["plugins"].items()
        )
    ):
        raise ValueError("Choose available extra tools.")
    if "progress" in changes and changes["progress"] not in ("default", "on", "off"):
        raise ValueError("Choose a progress setting.")
    if "ticket" in changes and type(changes["ticket"]) is not bool:
        raise ValueError("Choose whether ticket greetings are enabled.")
    paths = [
        data_dir / name
        for name in (
            "bot_control.json",
            "progress_servers.json",
            "progress_servers_off.json",
            "ticket_greeting_servers.json",
        )
    ]
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(FileLock(path, timeout=5))
        control = read_json(paths[0], dict, dict(fallback or {}))
        lists = [read_json(path, list, []) for path in paths[1:]]
        if "channel" in changes:
            mapping = dict(control.get("guild_solo_channel", {}))
            if changes["channel"]:
                mapping[guild_id] = changes["channel"]
            else:
                mapping.pop(guild_id, None)
            control["guild_solo_channel"] = mapping
            blocked = set(control.get("autonomy_blocked_servers", []))
            owned = set(control.get("guild_solo_autonomy_added", []))
            if changes["channel"] and guild_id not in blocked:
                blocked.add(guild_id)
                owned.add(guild_id)
            elif not changes["channel"] and guild_id in owned:
                blocked.discard(guild_id)
                owned.discard(guild_id)
            control.update(
                autonomy_blocked_servers=sorted(blocked),
                guild_solo_autonomy_added=sorted(owned),
            )
        for field, key, value in (
            (
                "capabilities",
                "guild_disabled_capabilities",
                sorted(set(GUILD_CAPABILITIES) - set(changes.get("capabilities", []))),
            ),
            ("plugins", "guild_plugin_overrides", changes.get("plugins", {})),
        ):
            if field in changes:
                mapping = dict(control.get(key, {}))
                if value:
                    mapping[guild_id] = value
                else:
                    mapping.pop(guild_id, None)
                control[key] = mapping
        if {"channel", "capabilities", "plugins"} & changes.keys():
            _atomic_json_write_sync(paths[0], control)
        if "progress" in changes:
            for index, setting in ((0, "on"), (1, "off")):
                values = set(lists[index])
                (values.add if changes["progress"] == setting else values.discard)(
                    guild_id
                )
                _atomic_json_write_sync(paths[index + 1], sorted(values))
        if "ticket" in changes:
            values = set(lists[2])
            (values.add if changes["ticket"] else values.discard)(guild_id)
            _atomic_json_write_sync(paths[3], sorted(values))
        return control


def merge_control_change(path: Path, key: str, value, before, fallback: dict) -> dict:
    """Discord's global-control writer must not overwrite web guild edits."""
    with FileLock(path, timeout=5):
        current = read_json(path, dict, dict(fallback))
        if (
            key.startswith("guild_")
            and isinstance(value, dict)
            and isinstance(before, dict)
        ):
            mapping = dict(current.get(key, {}))
            for guild_id in before.keys() | value.keys():
                if before.get(guild_id) != value.get(guild_id):
                    if guild_id in value:
                        mapping[guild_id] = value[guild_id]
                    else:
                        mapping.pop(guild_id, None)
            current[key] = mapping
        else:
            current[key] = value
        _atomic_json_write_sync(path, current)
        return current
