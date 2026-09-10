"""Fresh-process regressions for installer and API configuration failures."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from concurrency_safety import ToolConcurrency
from scripts.env_defaults import read_defaults
from scripts.set_env import set_env
from test_audit_install import _run_installer

ROOT = Path(__file__).resolve().parents[1]


def test_reconfigure_preserves_saved_provider_identity_and_disabled_shell(tmp_path):
    saved = {
        "DISCORD_TOKEN": "saved-token",
        "OLLAMA_BASE_URL": "https://provider.example/v1",
        "OLLAMA_MODEL": "my-model",
        "OLLAMA_API_KEY": 'key with "quotes" and # symbols',
        "BOT_NAME": "Custom Bot",
        "CREATOR_NAME": "Custom Owner",
        "CREATOR_ID": "123",
        "MAXWELL_OWNER_IDS": "123,456",
        "COMMAND_PREFIX": "!",
        "MAXWELL_ADMIN_USER": "operator",
        "MAXWELL_ADMIN_PASSWORD": "keep-this-password",
        "ENABLE_AUTONOMY": "true",
        "ENABLE_REM": "true",
        "ENABLE_SHELL": "false",
    }
    for name, value in saved.items():
        set_env(tmp_path / "saved.env", name, value)
    result = _run_installer(
        tmp_path, (tmp_path / "saved.env").read_text(), overrides={}, reconfigure=True
    )
    assert {name: result[name] for name in saved} == saved


def test_reconfigure_accepts_explicit_environment_overrides(tmp_path):
    result = _run_installer(
        tmp_path,
        "OLLAMA_MODEL=old\nOLLAMA_API_KEY=secret\nREM_ENABLED=true\nENABLE_REM=true\n",
        overrides={"OLLAMA_MODEL": "new", "OLLAMA_API_KEY": "", "REM_ENABLED": "false"},
        reconfigure=True,
    )
    assert result["OLLAMA_MODEL"] == "new"
    assert result["OLLAMA_API_KEY"] == ""
    assert result["REM_ENABLED"] == result["ENABLE_REM"] == "false"


def test_configure_only_needs_no_docker_and_does_not_start_services(tmp_path):
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    shutil.copyfile(ROOT / "install.sh", tmp_path / "install.sh")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    (tmp_path / "bot.py").touch()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\ntouch "$DOCKER_CALLED"\nexit 99\n')
    docker.chmod(0o755)
    marker = tmp_path / "docker-called"
    env = dict(
        os.environ,
        PATH=str(bindir) + os.pathsep + os.environ["PATH"],
        DOCKER_CALLED=str(marker),
        DISCORD_TOKEN="test-token",
        OLLAMA_MODEL="test-model",
    )
    result = subprocess.run(
        [
            "bash",
            str(tmp_path / "install.sh"),
            "--local",
            "--non-interactive",
            "--configure-only",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (tmp_path / "run.sh").is_file()
    assert dotenv_values(tmp_path / ".env")["OLLAMA_MODEL"] == "test-model"


def test_failed_image_build_does_not_stop_existing_host_service(tmp_path):
    source = (ROOT / "install.sh").read_text().removesuffix('main "$@"\n')
    driver = (
        source
        + """
rewrite_localhost_for_bridge() { :; }
resolve_compose() { COMPOSE=(fake_compose); }
fake_compose() { return 42; }
stop_host_maxwell() { touch stopped; }
start_stack
"""
    )
    result = subprocess.run(
        ["bash", "-c", driver], cwd=tmp_path, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 42
    assert not (tmp_path / "stopped").exists()


def test_defaults_are_literal_data_and_follow_dotenv_quoting(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "TOKEN=old\nexport TOKEN = 'literal $(touch marker) `id` ${HOME}' # comment\n"
        r'''OTHER="a\\b\"c\td"'''
        "\nBARE=value # comment\nEMPTY=\n"
    )
    expected = dotenv_values(path, interpolate=False)
    assert read_defaults(path, set(expected)) == expected
    assert not (tmp_path / "marker").exists()


def _api_settings(tmp_path, contents):
    env_file = tmp_path / "settings.env"
    env_file.write_text(contents)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("MAXWELL_", "REM_", "ENABLE_REM"))
    }
    env["MAXWELL_ENV_FILE"] = str(env_file)
    # Reproduce direct script execution: only api/, not the project root,
    # is initially importable. Do not start a real server or contact Discord.
    script = f"""
import sys, runpy, json
sys.path.insert(0, {str(ROOT / "api")!r})
ns = runpy.run_path({str(ROOT / "api/api_server.py")!r}, run_name='api_config_probe')
from api.config import REM_ENABLED_DEFAULT
print(json.dumps([
    ns['_API_MAX_CONCURRENT'], ns['_API_REQUEST_TIMEOUT'],
    ns['_API_GLOBAL_RPS'], ns['_API_GLOBAL_BURST'],
    ns['_API_GLOBAL_LIMITER'] is not None, REM_ENABLED_DEFAULT,
]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_direct_api_start_loads_dotenv_limits_and_rem_alias(tmp_path):
    assert _api_settings(
        tmp_path,
        "MAXWELL_API_MAX_CONCURRENT=17\nMAXWELL_API_REQUEST_TIMEOUT=2.5\n"
        "MAXWELL_API_GLOBAL_RPS=13\nMAXWELL_API_GLOBAL_BURST=19\nENABLE_REM=true\n",
    ) == [17, 2.5, 13, 19, True, True]


def test_api_bad_numeric_settings_do_not_crash_or_disable_limits(tmp_path):
    assert _api_settings(
        tmp_path,
        "MAXWELL_API_MAX_CONCURRENT=oops\nMAXWELL_API_REQUEST_TIMEOUT=nan\n"
        "MAXWELL_API_GLOBAL_RPS=inf\nMAXWELL_API_GLOBAL_BURST=oops\n"
        "ENABLE_REM=true\nREM_ENABLED=false\n",
    ) == [64, 30.0, 120.0, 240, True, False]


@pytest.mark.parametrize("value", ["0", "-1", "", "bad"])
def test_invalid_environment_concurrency_does_not_hang_or_crash(monkeypatch, value):
    monkeypatch.setenv("MAXWELL_CONCURRENCY_PROVIDER", value)

    async def run():
        gates = ToolConcurrency()
        assert (
            await asyncio.wait_for(
                gates.run("provider", asyncio.sleep(0, result=42), timeout=0.1), 1
            )
            == 42
        )

    asyncio.run(run())


@pytest.mark.parametrize("value", [0, -1])
def test_explicit_invalid_concurrency_fails_clearly(value):
    with pytest.raises(ValueError, match="must be positive"):
        ToolConcurrency(provider=value)
