"""Cross-module deployment contracts checked without live credentials."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


