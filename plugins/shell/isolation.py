"""Tenant identity and launch policy for untrusted shell workloads.

The Docker daemon is only an adapter here. Public shell execution requires
gVisor (runsc), an operator-provisioned egress network, and an external host
firewall marker. Ordinary containers are deliberately not an accepted fallback.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any


# Root needs a narrow in-guest capability set for package installation,
# ownership/permission changes, low-port development servers, and controlling
# its own processes. Everything else (including SYS_ADMIN, NET_ADMIN,
# SYS_MODULE, SYS_PTRACE, SYS_RAWIO, BPF, and SETPCAP) remains dropped.
GUEST_CAPABILITIES = (
    "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL",
    "SETGID", "SETUID", "NET_BIND_SERVICE", "SYS_CHROOT", "MKNOD", "SETFCAP",
)


@dataclass(frozen=True)
class ShellTenant:
    owner_id: str
    scope_id: str
    container_name: str


def shell_tenant(message: Any) -> ShellTenant | None:
    """Derive a stable tenant from the authenticated Discord event only."""
    author = getattr(message, "author", None)
    owner = str(getattr(author, "id", "") or "").strip()
    guild = getattr(message, "guild", None)
    guild_id = str(getattr(guild, "id", "") or "").strip()
    channel = getattr(message, "channel", None)
    channel_id = str(getattr(channel, "id", "") or "").strip()
    if not owner.isdigit():
        return None
    # Guild workspaces are per-user and per-server. Private interactions use
    # their trusted source channel/context key as well, so the same user does
    # not share one workspace across unrelated DMs or ephemeral app contexts.
    visibility = str(getattr(message, "response_visibility", "public") or "public")
    if visibility == "private":
        if not channel_id:
            return None
        scope = f"private:{guild_id if guild_id.isdigit() else 'dm'}:{channel_id}"
    elif guild_id.isdigit():
        scope = f"guild:{guild_id}"
    else:
        if not channel_id:
            return None
        scope = f"private:{channel_id}"
    digest = hashlib.sha256(f"{owner}\0{scope}".encode("utf-8")).hexdigest()[:24]
    return ShellTenant(
        owner_id=owner,
        scope_id=scope,
        container_name=f"mwsh-{digest}",
    )


def egress_marker_path() -> str:
    return os.environ.get(
        "MAXWELL_SHELL_EGRESS_MARKER", "/run/maxwell-shell-policy/egress-ready"
    )


def egress_policy_ready() -> bool:
    """The marker is mounted read-only from the host firewall provisioner."""
    try:
        with open(egress_marker_path(), "r", encoding="ascii") as marker:
            return marker.read(128).strip() == "maxwell-shell-egress-v2"
    except OSError:
        return False


def resource_pool_ready() -> bool:
    """The host provisioned the aggregate cgroup limits for all sandboxes."""
    try:
        path = os.path.join(os.path.dirname(egress_marker_path()), "resource-pool-ready")
        with open(path, "r", encoding="ascii") as marker:
            return marker.read(128).strip() == "maxwell-shell-resource-pool-v2"
    except OSError:
        return False


def validate_runsc_runtime(runtimes: Any) -> tuple[bool, str]:
    """Reject daemon runtime flags that bypass the Docker bridge policy."""
    if not isinstance(runtimes, dict):
        return False, "runtime list is not an object"
    runsc = runtimes.get("runsc")
    if not isinstance(runsc, dict):
        return False, "runsc is not configured"
    args = runsc.get("runtimeArgs", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        return False, "runsc runtimeArgs are invalid"
    platform = None
    network = None
    index = 0
    while index < len(args):
        arg = args[index].strip()
        if arg in {"--network", "--network-mode"}:
            index += 1
            if index >= len(args):
                return False, "runsc network option is incomplete"
            network = args[index].strip().lower()
        elif arg.startswith("--network="):
            network = arg.split("=", 1)[1].strip().lower()
        if network is not None and network != "sandbox":
            return False, f"runsc network mode {network!r} is not allowed"
        if arg.startswith("--platform="):
            platform = arg.split("=", 1)[1].strip().lower()
            if platform != "systrap":
                return False, f"runsc platform {platform!r} is not supported here"
        elif arg == "--platform":
            index += 1
            if index >= len(args) or args[index].strip().lower() != "systrap":
                return False, "runsc must use the systrap platform"
            platform = "systrap"
        index += 1
    if platform != "systrap":
        return False, "runsc must explicitly use the systrap platform"
    if network != "sandbox":
        return False, "runsc must explicitly use its isolated sandbox network stack"
    return True, "runsc configured without network bypass options"


def docker_run_args(
    *,
    container_name: str,
    image: str,
    runtime: str = "runsc",
    network: str = "maxwell-shell-egress",
) -> list[str]:
    """Construct the mandatory confined run configuration.

    Root is intentionally available inside the guest. Capabilities are dropped,
    privilege escalation is disabled, and syscall mediation is provided by
    runsc. The image's writable layer is capped by Docker's storage driver.
    """
    if runtime != "runsc":
        raise ValueError("the required gVisor runtime 'runsc' is not configured")
    if not re.fullmatch(r"mwsh-[a-f0-9]{24}", container_name):
        raise ValueError("invalid trusted sandbox name")
    if network != "maxwell-shell-egress":
        raise ValueError("the required filtered shell egress network is not configured")
    return [
        "run", "-d", "--init", "--runtime", runtime,
        "--name", container_name,
        "--cgroup-parent", "maxwell-shell.slice",
        "--label", "maxwell.shell.managed=true",
        "--label", "maxwell.shell.runtime=runsc",
        "--label", "maxwell.shell.policy=v2",
        "--user", "0:0",
        "--cap-drop", "ALL",
        *[arg for cap in GUEST_CAPABILITIES for arg in ("--cap-add", cap)],
        "--security-opt", "no-new-privileges:true",
        # Omitting seccomp=unconfined retains Docker's default profile; runsc
        # adds its userspace syscall implementation and confinement.
        "--memory", "2g", "--memory-swap", "2g",
        "--cpus", "1.0", "--pids-limit", "256",
        "--ulimit", "nofile=1024:2048",
        "--storage-opt", "size=6G",
        "--network", network,
        "--tmpfs", "/workspace:rw,exec,nosuid,nodev,size=512m",
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m",
        image,
    ]


__all__ = [
    "GUEST_CAPABILITIES", "ShellTenant", "docker_run_args", "egress_policy_ready", "resource_pool_ready",
    "validate_runsc_runtime",
    "shell_tenant",
]
