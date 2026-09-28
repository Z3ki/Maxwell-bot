from __future__ import annotations

import json
import stat
from pathlib import Path

from scripts import inventory_site_backends as inventory


def test_container_inventory_omits_environment_and_logs(monkeypatch):
    calls = []
    row = {
        "Id": "a" * 64,
        "Name": "/maxwell-site-alpha",
        "Config": {
            "Image": "maxwell-siteimg-alpha",
            "Labels": {"maxwell.site": "alpha"},
            "Env": ["API_SECRET=do-not-export"],
        },
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped"},
            "Memory": 268435456,
            "MemorySwap": 268435456,
            "PidsLimit": 128,
        },
        "State": {"Status": "running", "RestartCount": 2},
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/srv/maxwell/site_servers/alpha",
                "Destination": "/app",
                "RW": False,
            }
        ],
        "NetworkSettings": {
            "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8800"}]},
            "Networks": {"bridge": {}},
        },
    }

    def fake_docker(*args):
        calls.append(args)
        if args[:2] == ("ps", "-a") and "--filter" not in args:
            return "maxwell-site-alpha\nunrelated-container\n"
        if args[:2] == ("ps", "-a"):
            return ""
        if args[0] == "inspect":
            assert args[1:] == ("maxwell-site-alpha",)
            return json.dumps([row])
        raise AssertionError(f"unexpected docker command: {args}")

    monkeypatch.setattr(inventory, "_docker", fake_docker)
    result = inventory._container_inventory()

    assert [item["name"] for item in result] == ["maxwell-site-alpha"]
    assert result[0]["site_slug_label"] == "alpha"
    assert result[0]["restart_policy"] == "unless-stopped"
    assert result[0]["mounts"][0]["source"] == "/srv/maxwell/site_servers/alpha"
    assert "API_SECRET" not in json.dumps(result)
    assert not any(args[0] in {"run", "rm", "build", "exec", "create"} for args in calls)


def test_private_inventory_file_is_created_mode_0600(tmp_path: Path):
    output = tmp_path / "private" / "inventory.json"
    inventory._write_report({"read_only": True}, output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text()) == {"read_only": True}


def test_site_data_inventory_reports_only_counts_and_sizes(tmp_path: Path):
    site = tmp_path / "site_servers" / "demo"
    site.mkdir(parents=True)
    (site / "app.py").write_text("secret source text", encoding="utf-8")
    (site / "_data.sqlite3").write_bytes(b"1234")

    result = inventory._site_data_inventory(tmp_path)
    assert result == [{"site_slug": "demo", "file_count": 2, "total_bytes": 22}]
    assert "app.py" not in json.dumps(result)
    assert "secret source text" not in json.dumps(result)
