"""Read-only offline activation preflight. No provider request or Discord login."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from standby.policy import PolicyDenied, parse_json, read_credential, validate_provider
from standby.sync import snowflake, validate_snapshot


def validate(root: Path, *, now: float | None = None) -> None:
    validate_provider(parse_json(read_credential(root / "provider.json")))
    if not read_credential(root / "discord-token").strip():
        raise PolicyDenied("Missing Discord credential")
    snapshot = validate_snapshot(
        parse_json(read_credential(root / "chat-config.json")), now=now
    )
    if (time.time() if now is None else now) - snapshot["exported_at"] > 900:
        raise PolicyDenied(
            "Refresh scoped config snapshot before activation (15 minute limit)"
        )
    text = read_credential(root / "audience.env").strip()
    prefix = "MAXWELL_STANDBY_CHANNEL_IDS="
    if not text.startswith(prefix):
        raise PolicyDenied("Explicit audience required")
    channels = text[len(prefix) :].split(",")
    if not channels or len(channels) > 10000:
        raise PolicyDenied("Invalid audience")
    for item in channels:
        snowflake(item)
    if any(importlib.util.find_spec(name) is None for name in ("discord", "aiohttp")):
        raise PolicyDenied("Required runtime dependencies are not installed")


if __name__ == "__main__":
    validate(Path("/etc/maxwell-standby"))
    print("Offline standby preflight OK (does not prove live credentials or gateway)")
