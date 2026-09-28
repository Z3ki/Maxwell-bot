#!/usr/bin/env python3
"""Read-only inventory for planning legacy site-backend migration.

The report deliberately excludes container environment variables, logs, and
file contents. It includes host mount paths and aggregate site-data sizes, so
write the report to an operator-private location.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(message[:500] or f"docker {args[0]} failed")
    return result.stdout


def _container_inventory() -> list[dict[str, Any]]:
    names = {
        name.strip()
        for name in _docker("ps", "-a", "--format", "{{.Names}}").splitlines()
        if name.strip().startswith("maxwell-site-")
    }
    names.update(
        name.strip()
        for name in _docker(
            "ps", "-a", "--filter", "label=maxwell.site", "--format", "{{.Names}}"
        ).splitlines()
        if name.strip()
    )
    if not names:
        return []

    rows = json.loads(_docker("inspect", *sorted(names)))
    inventory = []
    for row in rows:
        config = row.get("Config") or {}
        host = row.get("HostConfig") or {}
        state = row.get("State") or {}
        labels = config.get("Labels") or {}
        mounts = [
            {
                "type": item.get("Type"),
                "source": item.get("Source"),
                "destination": item.get("Destination"),
                "read_write": bool(item.get("RW")),
            }
            for item in row.get("Mounts") or []
        ]
        networks = sorted(
            (row.get("NetworkSettings", {}).get("Networks") or {}).keys()
        )
        ports = {
            port: [
                {"host_ip": binding.get("HostIp"), "host_port": binding.get("HostPort")}
                for binding in bindings
            ]
            for port, bindings in (row.get("NetworkSettings", {}).get("Ports") or {}).items()
            if bindings
        }
        inventory.append(
            {
                "id": str(row.get("Id") or "")[:12],
                "name": str(row.get("Name") or "").lstrip("/"),
                "site_slug_label": labels.get("maxwell.site"),
                "image": config.get("Image"),
                "created": row.get("Created"),
                "state": state.get("Status"),
                "restart_count": state.get("RestartCount"),
                "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
                "resource_limits": {
                    "memory_bytes": host.get("Memory"),
                    "memory_swap_bytes": host.get("MemorySwap"),
                    "nano_cpus": host.get("NanoCpus"),
                    "cpu_quota": host.get("CpuQuota"),
                    "cpu_period": host.get("CpuPeriod"),
                    "pids_limit": host.get("PidsLimit"),
                },
                "ports": ports,
                "networks": networks,
                "mounts": mounts,
            }
        )
    return sorted(inventory, key=lambda item: item["name"])


def _image_inventory() -> list[dict[str, str]]:
    rows = []
    for line in _docker(
        "image", "ls", "--no-trunc", "--format",
        "{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedAt}}",
    ).splitlines():
        fields = line.split("\t", maxsplit=4)
        if len(fields) != 5:
            continue
        repository, tag, image_id, size, created = fields
        if repository == "maxwell-site-runtime" or repository.startswith("maxwell-siteimg-"):
            rows.append(
                {
                    "repository": repository,
                    "tag": tag,
                    "id": image_id[:19],
                    "size": size,
                    "created": created,
                }
            )
    return rows


def _site_data_inventory(data_dir: Path | None) -> list[dict[str, Any]]:
    if data_dir is None:
        return []
    root = data_dir / "site_servers"
    if not root.is_dir():
        return []
    rows = []
    for site_dir in sorted(root.iterdir()):
        if not site_dir.is_dir() or site_dir.is_symlink():
            continue
        file_count = 0
        total_bytes = 0
        for current, dirs, files in os.walk(site_dir, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
            for name in files:
                path = Path(current) / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    file_count += 1
                    total_bytes += path.stat().st_size
                except OSError:
                    continue
        rows.append(
            {"site_slug": site_dir.name, "file_count": file_count, "total_bytes": total_bytes}
        )
    return rows


def _write_report(report: dict[str, Any], output: Path | None) -> None:
    content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(content)
        return
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(output, flags, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DATA_DIR"),
        help="Maxwell DATA_DIR, for aggregate site file counts and byte sizes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="private report path (mode 0600); stdout is used if omitted",
    )
    args = parser.parse_args()
    try:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "environment_variables_included": False,
            "container_logs_included": False,
            "containers": _container_inventory(),
            "images": _image_inventory(),
            "site_data": _site_data_inventory(Path(args.data_dir) if args.data_dir else None),
        }
        _write_report(report, args.output)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"inventory failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
