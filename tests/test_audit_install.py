"""Offline regressions for install, runtime checks, and configuration."""

import builtins
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

import doctor
from docker.healthcheck import bot_is_running
from scripts.rewrite_bridge_env import rewrite_bridge_env
from scripts.set_env import set_env

ROOT = Path(__file__).resolve().parents[1]


def _run_installer(tmp_path, existing=None, *, overrides=None, reconfigure=False):
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    (tmp_path / "bot.py").touch()
    if existing is not None:
        (tmp_path / ".env").write_text(existing)
    source = (ROOT / "install.sh").read_text()
    assert source.endswith('main "$@"\n')
    driver = tmp_path / "install.sh"
    driver.write_text(
        source.removesuffix('main "$@"\n')
        + """
banner_and_confirm() { :; }
install_docker() { :; }
start_stack() { :; }
run_doctor() { :; }
final_summary() { :; }
main "$@"
"""
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            (
                "MAXWELL_",
                "DISCORD_",
                "OLLAMA_",
                "ENABLE_",
                "CREATOR_",
                "BOT_",
                "REM_",
                "COMMAND_PREFIX",
            )
        )
    }
    env.update(
        overrides
        if overrides is not None
        else {
            "DISCORD_BOT_TOKEN": "test-token",
            "OLLAMA_MODEL": "test-model",
            "MAXWELL_OWNER_IDS": "123",
            "MAXWELL_ADMIN_PASSWORD": "test-password",
        }
    )
    result = subprocess.run(
        ["bash", str(driver), "--local", "--non-interactive"]
        + (["--reconfigure"] if reconfigure else []),
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return dotenv_values(tmp_path / ".env")


def test_installer_detects_missing_controlling_terminal():
    source = (ROOT / "install.sh").read_text().removesuffix('main "$@"\n')
    result = subprocess.run(
        ["bash", "-c", source + '\nprintf "mode=%s" "$NONINTERACTIVE"'],
        env=dict(os.environ, MAXWELL_NONINTERACTIVE="0"),
        start_new_session=True,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("mode=1")


def test_first_install_runs_configuration_wizard(tmp_path):
    values = _run_installer(tmp_path)
    assert values["DISCORD_BOT_TOKEN"] == "test-token"
    assert values["OLLAMA_MODEL"] == "test-model"
    assert values["MAXWELL_ADMIN_PASSWORD"] == "test-password"
    assert values["MAXWELL_OWNER_IDS"] == "123"
    assert values["ENABLE_AUTONOMY"] == "false"
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_existing_install_keeps_configuration_without_reconfigure(tmp_path):
    values = _run_installer(
        tmp_path, "DISCORD_TOKEN=existing\nOLLAMA_MODEL=existing-model\n"
    )
    assert values["DISCORD_TOKEN"] == "existing"
    assert values["OLLAMA_MODEL"] == "existing-model"


def test_bridge_rewrite_changes_only_local_connection_addresses(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# localhost is described here\n"
        'OLLAMA_BASE_URL="http://user:localhost@localhost:11434/v1?x=127.0.0.1"\n'
        "AUX_BASE_URL=https://localhost.example/v1\n"
        "X_API_BASE_URL=https://remote.example/localhost\n"
        "MAXWELL_EMBED_BASE_URL=http://127.0.0.1:11434\n"
        "MAXWELL_SMTP_HOST=127.0.0.1\n"
        'MAXWELL_IMAP_HOST="localhost" # mailbox\n'
        "MAXWELL_API_HOST=127.0.0.1\n"
        "CAPTCHA_HUMAN_HOST=127.0.0.1\n"
        "OLLAMA_API_KEY=secret-localhost-127.0.0.1\n"
        "MAXWELL_ADMIN_PASSWORD=localhost\n"
    )
    rewrite_bridge_env(path)
    result = path.read_text()
    values = dotenv_values(path)
    assert (
        values["OLLAMA_BASE_URL"]
        == "http://user:localhost@host.docker.internal:11434/v1?x=127.0.0.1"
    )
    assert values["MAXWELL_EMBED_BASE_URL"] == "http://host.docker.internal:11434"
    assert values["MAXWELL_SMTP_HOST"] == "host.docker.internal"
    assert values["MAXWELL_IMAP_HOST"] == "host.docker.internal"
    assert values["MAXWELL_API_HOST"] == "0.0.0.0"
    assert values["CAPTCHA_HUMAN_HOST"] == "127.0.0.1"
    assert values["AUX_BASE_URL"] == "https://localhost.example/v1"
    assert values["X_API_BASE_URL"] == "https://remote.example/localhost"
    assert values["OLLAMA_API_KEY"] == "secret-localhost-127.0.0.1"
    assert values["MAXWELL_ADMIN_PASSWORD"] == "localhost"
    assert "# localhost is described here" in result
    rewrite_bridge_env(path)
    assert path.read_text() == result


def test_env_writer_updates_duplicate_and_spaced_assignments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TOKEN=old\n  export TOKEN = stale\nTOKEN=last\nOTHER=keep\n")
    set_env(path, "TOKEN", r"new\1 #secret")
    values = dotenv_values(path)
    assert values["TOKEN"] == r"new\1 #secret"
    assert values["OTHER"] == "keep"
    assert "stale" not in path.read_text()


def test_env_writer_rejects_newline_in_key(tmp_path):
    with pytest.raises(ValueError, match="invalid environment key"):
        set_env(tmp_path / ".env", "TOKEN\n", "value")


def test_blank_env_file_and_site_dir_use_defaults(tmp_path):
    env = dict(os.environ, MAXWELL_ENV_FILE="", MAXWELL_SITE_DIR="")
    # Copy only config to avoid reading a real checkout's .env.
    shutil.copyfile(ROOT / "config.py", tmp_path / "config.py")
    (tmp_path / ".env").write_text("OLLAMA_MODEL=from-file\nMAXWELL_SITE_DIR=\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config,json; print(json.dumps([str(config.ENV_FILE), config.Config.OLLAMA_MODEL, config.Config.MAXWELL_SITE_DIR]))",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        str(tmp_path / ".env"),
        "from-file",
        "public/bot",
    ]


def test_doctor_system_tools_does_not_require_config_import(monkeypatch, capsys):
    real_import = builtins.__import__

    def import_without_config(name, *args, **kwargs):
        if name == "config":
            raise ModuleNotFoundError("No module named 'dotenv'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_config)
    doctor.check_system_tools()
    assert "Optional system tools" in capsys.readouterr().out


def test_doctor_checks_chess_and_pillow(monkeypatch):
    monkeypatch.setattr(doctor, "problems", [])
    monkeypatch.setattr(doctor, "find_spec", lambda name: name not in {"chess", "PIL"})
    doctor.check_core_packages()
    assert "pip install python-chess" in doctor.problems
    assert "pip install pillow" in doctor.problems


def _cmdline(proc_root, pid, value):
    directory = proc_root / str(pid)
    directory.mkdir()
    (directory / "cmdline").write_bytes(value)


def test_healthcheck_ignores_own_code_and_unrelated_processes(tmp_path):
    _cmdline(tmp_path, 1, b"python3\0-c\0any(b'bot.py' in p.read_bytes())\0")
    _cmdline(tmp_path, 2, b"grep\0bot.py\0")
    _cmdline(tmp_path, 3, b"python3\0other_bot.py\0")
    assert not bot_is_running(tmp_path)


@pytest.mark.parametrize("script", [b"bot.py", b"/app/bot.py"])
def test_healthcheck_finds_actual_python_bot(tmp_path, script):
    _cmdline(tmp_path, 4, b"/usr/local/bin/python3\0" + script + b"\0")
    assert bot_is_running(tmp_path)


def test_healthcheck_tolerates_exited_process(tmp_path, monkeypatch):
    _cmdline(tmp_path, 5, b"")
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert not bot_is_running(tmp_path)


def test_numpy_requirement_supports_python311_and312():
    from packaging.requirements import Requirement

    requirements = [
        Requirement(line.split("#", 1)[0].strip())
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for python_version, numpy_version in [("3.11", "2.3.5"), ("3.12", "2.5.1")]:
        candidates = [
            r
            for r in requirements
            if r.name == "numpy"
            and (
                r.marker is None
                or r.marker.evaluate({"python_version": python_version})
            )
        ]
        assert len(candidates) == 1
        assert numpy_version in candidates[0].specifier
