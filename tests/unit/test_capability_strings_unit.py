"""Regression: a single capability string is one capability, not its letters."""

from __future__ import annotations

from maxwell_core import PLUGIN_API_VERSION
from maxwell_core.capabilities import missing_capabilities, normalize_capabilities
from maxwell_core.plugins.manifest import validate_manifest


def test_bare_string_is_one_capability():
    assert normalize_capabilities("shell") == ["shell"]
    assert normalize_capabilities("  network ") == ["network"]
    assert normalize_capabilities("") == []


def test_missing_capabilities_with_string_arguments():
    assert missing_capabilities("network", "network") == []
    assert missing_capabilities("network", "shell") == ["shell"]


def test_manifest_string_permissions_parse_as_single_names():
    manifest = validate_manifest(
        {
            "id": "demo",
            "api_version": PLUGIN_API_VERSION,
            "permissions": "network",
            "requires": "shell",
            "provides": "jobs.run",
        },
        directory_name="demo",
    )
    assert manifest.permissions == ["network"]
    assert manifest.required_capabilities == ["shell"]
    assert manifest.provided_capabilities == ["jobs.run"]
