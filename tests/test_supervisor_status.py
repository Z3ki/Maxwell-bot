from pathlib import Path

import pytest

from api import api_server


@pytest.fixture
def supervised_processes(tmp_path, monkeypatch):
    app_root = tmp_path / "checkout"
    (app_root / "docker").mkdir(parents=True)
    proc_root = tmp_path / "proc"
    parent = proc_root / "10"
    (parent / "task" / "10").mkdir(parents=True)
    (parent / "cwd").symlink_to(app_root, target_is_directory=True)
    (parent / "task" / "10" / "children").write_text("20 21")
    for pid, script in (("20", "api/api_server.py"), ("21", "bot.py")):
        child = proc_root / pid
        child.mkdir()
        (child / "cmdline").write_bytes(
            b"python3\0" + str(app_root / script).encode() + b"\0"
        )
        (child / "stat").write_text(f"{pid} (python3) S 10 0 0")
    monkeypatch.setattr(api_server, "APP_ROOT", app_root)
    monkeypatch.setattr(api_server.os, "getppid", lambda: 10)
    monkeypatch.setattr(
        api_server, "Path", lambda value: proc_root if value == "/proc" else Path(value)
    )
    return app_root, proc_root


@pytest.mark.parametrize("launch_path", ["relative", "dot_relative", "absolute"])
def test_supervisor_launch_path_does_not_change_bot_health(supervised_processes, launch_path):
    app_root, proc_root = supervised_processes
    script = {
        "relative": "docker/supervisor.py",
        "dot_relative": "./docker/supervisor.py",
        "absolute": str(app_root / "docker" / "supervisor.py"),
    }[launch_path]
    (proc_root / "10" / "cmdline").write_bytes(b"python3\0" + script.encode() + b"\0")
    assert api_server._supervised_bot_is_running()


def test_health_rejects_unrelated_parent_and_bot_name_in_arguments(supervised_processes):
    app_root, proc_root = supervised_processes
    parent_cmdline = proc_root / "10" / "cmdline"
    parent_cmdline.write_bytes(b"python3\0some_other_program.py\0")
    assert not api_server._supervised_bot_is_running()
    parent_cmdline.write_bytes(b"python3\0docker/supervisor.py\0")
    (proc_root / "21" / "cmdline").write_bytes(
        b"python3\0-c\0print('unrelated')\0" + str(app_root / "bot.py").encode() + b"\0"
    )
    assert not api_server._supervised_bot_is_running()


@pytest.mark.parametrize("dead_state", ["Z", "X"])
def test_health_clears_when_bot_dies_or_disappears(supervised_processes, dead_state):
    _, proc_root = supervised_processes
    (proc_root / "10" / "cmdline").write_bytes(b"python3\0docker/supervisor.py\0")
    assert api_server._supervised_bot_is_running()
    (proc_root / "21" / "stat").write_text(f"21 (python3) {dead_state} 10 0 0")
    assert not api_server._supervised_bot_is_running()
    (proc_root / "21" / "cmdline").unlink()
    assert not api_server._supervised_bot_is_running()
