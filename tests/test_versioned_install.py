"""Execute both installers against local releases without starting services."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    "install.sh", "easy-install.sh", ".env.example", ".env.simple.example",
    "scripts/set_env.py", "scripts/env_defaults.py", "scripts/migrate_ai_env.py",
    "scripts/rewrite_bridge_env.py", "docker/maxwell.Dockerfile",
    "docker/entrypoint.sh", "docker/supervisor.py", "docker-compose.yml",
    "docker-compose.bridge.yml",
)


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_remote(path):
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "installer-test@example.invalid")
    _git(path, "config", "user.name", "Installer test")
    for name in INPUTS:
        destination = path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    (path / ".gitignore").write_text(".env\nrun.sh\ndata/\npublic/\nlogs/\nshelldocker/\n")
    (path / "bot.py").write_text("RELEASE = 'one'\n")
    _git(path, "add", "-f", *INPUTS, ".gitignore", "bot.py")
    _git(path, "commit", "-m", "First release")
    first = _git(path, "rev-parse", "HEAD")
    _git(path, "tag", "-a", "v0.1.0", "-m", "First release")
    (path / "bot.py").write_text("RELEASE = 'two'\n")
    _git(path, "commit", "-am", "Second release")
    second = _git(path, "rev-parse", "HEAD")
    _git(path, "tag", "v0.2.0")
    return first, second


def _install(script, remote, destination, *selection):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("MAXWELL_", "OLLAMA_", "AI_", "DISCORD_", "ENABLE_", "REM_"))
    }
    env.update(
        MAXWELL_REPO_URL=str(remote), DISCORD_BOT_TOKEN="fixture-token",
        AI_API_URL="http://127.0.0.1:9999/v1", AI_MODEL="fixture-model",
        AI_API_KEY="", MAXWELL_ADMIN_PASSWORD="fixture-password",
    )
    return subprocess.run(
        ["bash", str(ROOT / script), "--dir", str(destination),
         "--non-interactive", "--configure-only", *selection],
        env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("script", ["easy-install.sh", "install.sh"])
def test_pinned_tag_reinstall_and_upgrade_preserve_persistent_state(tmp_path, script):
    remote = tmp_path / "remote"
    first, second = _make_remote(remote)
    destination = tmp_path / "installed"
    result = _install(script, remote, destination, "--version", "0.1.0")
    assert result.returncode == 0, result.stderr
    assert _git(destination, "rev-parse", "HEAD") == first
    assert "one" in (destination / "bot.py").read_text()
    assert dotenv_values(destination / ".env")["AI_MODEL"] == "fixture-model"
    assert (destination / ".env").stat().st_mode & 0o777 == 0o600
    retained = {
        ".env": (destination / ".env").read_bytes(),
        "data/state.json": b'{"durable":"memory"}',
        "public/bot/site/index.html": b"a generated site",
    }
    for name, content in retained.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for version, expected in (("v0.1.0", first), ("v0.2.0", second)):
        result = _install(script, remote, destination, "--version", version)
        assert result.returncode == 0, result.stderr
        assert _git(destination, "rev-parse", "HEAD") == expected
        assert {name: (destination / name).read_bytes() for name in retained} == retained
    assert (destination / "run.sh").stat().st_mode & 0o111


@pytest.mark.parametrize("script", ["easy-install.sh", "install.sh"])
@pytest.mark.parametrize("selection", [[], ["--ref"]])
def test_development_default_and_exact_commit_resolve_immutably(tmp_path, script, selection):
    remote = tmp_path / "remote"
    first, second = _make_remote(remote)
    destination = tmp_path / "installed"
    args = [*selection, first] if selection else []
    result = _install(script, remote, destination, *args)
    assert result.returncode == 0, result.stderr
    assert _git(destination, "rev-parse", "HEAD") == (first if selection else second)
    assert _git(destination, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


@pytest.mark.parametrize("script", ["easy-install.sh", "install.sh"])
@pytest.mark.parametrize("selection", [
    ["--version", "v9.9.9"], ["--version", "../main"],
    ["--ref", "f" * 40], ["--version", "v0.1.0", "--ref", "a" * 40],
])
def test_bad_selection_does_not_replace_existing_install(tmp_path, script, selection):
    remote = tmp_path / "remote"
    first, _ = _make_remote(remote)
    destination = tmp_path / "installed"
    assert _install(script, remote, destination, "--version", "v0.1.0").returncode == 0
    env_before = (destination / ".env").read_bytes()
    result = _install(script, remote, destination, *selection)
    assert result.returncode != 0
    assert _git(destination, "rev-parse", "HEAD") == first
    assert (destination / ".env").read_bytes() == env_before


@pytest.mark.parametrize("script", ["easy-install.sh", "install.sh"])
def test_dirty_tracked_files_and_incomplete_release_are_preserved(tmp_path, script):
    remote = tmp_path / "remote"
    first, _ = _make_remote(remote)
    destination = tmp_path / "installed"
    assert _install(script, remote, destination, "--version", "v0.1.0").returncode == 0
    local_work = "RELEASE = 'local work'\n"
    (destination / "bot.py").write_text(local_work)
    assert _install(script, remote, destination, "--version", "v0.2.0").returncode != 0
    assert (destination / "bot.py").read_text() == local_work
    (destination / "bot.py").write_text("RELEASE = 'one'\n")
    _git(remote, "rm", "scripts/set_env.py")
    _git(remote, "commit", "-m", "Incomplete release")
    incomplete = _git(remote, "rev-parse", "HEAD")
    assert _install(script, remote, destination, "--ref", incomplete).returncode != 0
    assert _git(destination, "rev-parse", "HEAD") == first
    fresh = tmp_path / "fresh"
    assert _install(script, remote, fresh, "--ref", incomplete).returncode != 0
    assert not fresh.exists()
