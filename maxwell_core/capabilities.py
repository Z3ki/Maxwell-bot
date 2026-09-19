"""Capability names plugins may request and tools may require.

Python plugins run in-process. A manifest permission is an operator
declaration and a runtime check, not a sandbox. Malicious plugin code can
still import anything the process can import. Document that distinction
wherever plugin installation is exposed.
"""

from __future__ import annotations

from typing import Iterable

# Canonical capability identifiers. Keep these stable; plugins and
# manifests refer to them by string.
MESSAGES_READ = "messages.read"
MESSAGES_SEND = "messages.send"
MEMORY_READ = "memory.read"
MEMORY_WRITE = "memory.write"
DISCORD_CHANNELS = "discord.channels"
DISCORD_MODERATE = "discord.moderate"
FILES_READ = "files.read"
FILES_WRITE = "files.write"
NETWORK = "network"
SHELL = "shell"
PLUGINS_MANAGE = "plugins.manage"
CONFIG_MODIFY = "config.modify"
SECRETS_READ = "secrets.read"
JOBS_RUN = "jobs.run"
VOICE = "voice"

ALL_CAPABILITIES: frozenset[str] = frozenset(
    {
        MESSAGES_READ,
        MESSAGES_SEND,
        MEMORY_READ,
        MEMORY_WRITE,
        DISCORD_CHANNELS,
        DISCORD_MODERATE,
        FILES_READ,
        FILES_WRITE,
        NETWORK,
        SHELL,
        PLUGINS_MANAGE,
        CONFIG_MODIFY,
        SECRETS_READ,
        JOBS_RUN,
        VOICE,
    }
)

# Human labels for dashboard / Discord plugin management.
CAPABILITY_LABELS: dict[str, str] = {
    MESSAGES_READ: "Read messages",
    MESSAGES_SEND: "Send messages",
    MEMORY_READ: "Read permitted memory",
    MEMORY_WRITE: "Write memory",
    DISCORD_CHANNELS: "Manage Discord channels",
    DISCORD_MODERATE: "Moderate members",
    FILES_READ: "Read files",
    FILES_WRITE: "Write files",
    NETWORK: "Access the network",
    SHELL: "Execute shell commands",
    PLUGINS_MANAGE: "Manage plugins",
    CONFIG_MODIFY: "Modify configuration",
    SECRETS_READ: "Access secrets",
    JOBS_RUN: "Run background jobs",
    VOICE: "Voice / TTS",
}

# Privileged capabilities that the dashboard must warn about.
PRIVILEGED_CAPABILITIES: frozenset[str] = frozenset(
    {
        SHELL,
        SECRETS_READ,
        PLUGINS_MANAGE,
        CONFIG_MODIFY,
        DISCORD_MODERATE,
        FILES_WRITE,
    }
)


def normalize_capabilities(values: Iterable[str] | None) -> list[str]:
    """Return unique, known capability names. Unknown names are kept (forward-compat)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def missing_capabilities(
    granted: Iterable[str] | None, required: Iterable[str] | None
) -> list[str]:
    have = set(normalize_capabilities(granted))
    return [cap for cap in normalize_capabilities(required) if cap not in have]
