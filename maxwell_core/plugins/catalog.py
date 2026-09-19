"""Scan plugin directories for manifests without importing plugin code.

Used by the dashboard, ``KNOWN_TOOLS``, and doctor-style diagnostics so the
API process does not have to construct MaxwellBot to list tools.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .manifest import ManifestError, PluginManifest, load_manifest_file

_ROOT = Path(__file__).resolve().parents[2]


def plugin_search_paths(extra: Iterable[str | Path] | None = None) -> list[Path]:
    paths: list[Path] = [_ROOT / "plugins"]
    env = os.getenv("MAXWELL_PLUGIN_PATH", "").strip()
    if env:
        for part in env.split(os.pathsep):
            part = part.strip()
            if part:
                paths.append(Path(part))
    installed = _ROOT / "data" / "installed_plugins"
    paths.append(installed)
    paths.extend(Path(item) for item in extra or ())
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def iter_plugin_dirs(extra: Iterable[str | Path] | None = None) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in plugin_search_paths(extra):
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            if not entry.name.isidentifier():
                continue
            if entry.name in seen:
                continue
            seen.add(entry.name)
            found.append(entry)
    return found


def discover_plugin_manifests(
    extra: Iterable[str | Path] | None = None,
) -> list[tuple[Path, PluginManifest | None, str]]:
    """Return (dir, manifest_or_none, error_message)."""
    results: list[tuple[Path, PluginManifest | None, str]] = []
    for entry in iter_plugin_dirs(extra):
        manifest_path = entry / "plugin.json"
        if not manifest_path.is_file():
            results.append((entry, None, "missing plugin.json"))
            continue
        try:
            manifest = load_manifest_file(manifest_path, directory_name=entry.name)
        except ManifestError as exc:
            results.append((entry, None, str(exc)))
            continue
        results.append((entry, manifest, ""))
    return results


def discover_tool_names(extra: Iterable[str | Path] | None = None) -> list[str]:
    """Stable unique tool names declared in manifests."""
    names: list[str] = []
    seen: set[str] = set()
    for _path, manifest, error in discover_plugin_manifests(extra):
        if error or manifest is None:
            continue
        for name in manifest.tool_names():
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names
