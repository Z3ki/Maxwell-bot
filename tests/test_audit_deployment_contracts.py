"""Cross-module deployment contracts checked without live credentials."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.api_server as api

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("explicit_base", [True, False])
def test_oauth_returns_to_dashboard_not_generated_site(monkeypatch, explicit_base):
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://sites.example.test")
    monkeypatch.setenv(
        "DISCORD_REDIRECT_URI", "https://admin.example.test/api/auth/discord/callback"
    )
    if explicit_base:
        monkeypatch.setenv("DISCORD_REDIRECT_BASE", "https://admin.example.test/")
    else:
        monkeypatch.delenv("DISCORD_REDIRECT_BASE", raising=False)
    request = SimpleNamespace(scheme="https", host="untrusted.example.test")
    assert api._discord_redirect_base(request) == "https://admin.example.test"


def test_api_empty_template_paths_use_checkout_defaults(tmp_path):
    env = os.environ.copy()
    env.update(MAXWELL_APP_ROOT=str(tmp_path), MAXWELL_ENV_FILE="", MAXWELL_SITE_DIR="")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from api.storage import ENV_FILE; from api.config import BASE_SITE_DIR; print(json.dumps([str(ENV_FILE),str(BASE_SITE_DIR)]))",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        str(tmp_path / ".env"),
        str(tmp_path / "public/bot"),
    ]


@pytest.mark.parametrize("custom_pm2_home", [True, False])
def test_pm2_logs_follow_invoking_user(tmp_path, custom_pm2_home):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), MAXWELL_PM2_OLLAMA="false")
    env.pop("PM2_HOME", None)
    log_root = tmp_path / ".pm2/logs"
    if custom_pm2_home:
        env["PM2_HOME"] = str(tmp_path / "custom")
        log_root = tmp_path / "custom/logs"
    result = subprocess.run(
        [
            node,
            "-e",
            "console.log(JSON.stringify(require('./ecosystem.config.js').apps))",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    for app in json.loads(result.stdout):
        assert Path(app["out_file"]).parent == log_root
        assert Path(app["error_file"]).parent == log_root


def test_docker_desktop_has_a_linux_docker_cli():
    dockerfile = (ROOT / "docker/maxwell.Dockerfile").read_text()
    assert "FROM docker:28-cli AS docker-cli" in dockerfile
    assert "COPY --from=docker-cli /usr/local/bin/docker /usr/bin/docker" in dockerfile


def test_caddy_separates_dashboard_and_generated_site_roots():
    caddyfile = (ROOT / "examples/Caddyfile.example").read_text()
    admin, public = caddyfile.split("\nmaxwell.example.com {", 1)
    assert "admin.maxwell.example.com {" in admin
    assert "handle /api/*" in admin
    assert "handle @admin" in admin
    assert "handle /bot/" not in admin
    assert "root * /var/www/maxwell/bot" in public
    assert "handle_path /bot/*" in public
    assert "handle /api/*" not in public
    assert 'respond "Not found" 404' in public
