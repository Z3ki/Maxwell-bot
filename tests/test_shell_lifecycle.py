"""Fail-closed startup, restart recovery, and timeout cleanup for shell guests."""

from __future__ import annotations

import asyncio
import re

import pytest

from plugins.shell.impl import ShellTool
from plugins.shell.isolation import ShellTenant


def test_large_script_and_both_output_streams_survive_capture(monkeypatch):
    tenant = ShellTenant("7", "guild:9", "mwsh-" + "a" * 24)
    tool = ShellTool(None)
    create_process = asyncio.create_subprocess_exec
    script_paths = []

    async def local_process(*args, **kwargs):
        assert args[:3] == ("docker", "exec", "-i")
        bootstrap = args[-1]
        assert len(bootstrap) < 1000
        script_paths.extend(re.findall(r"/tmp/maxwell-exec-[a-f0-9]+\.pid(?:\.sh)?", bootstrap))
        # Test the actual stdin transport and concurrent pipe pumps without
        # needing Docker. Only this fixed test script executes locally.
        return await create_process("bash", "-c", bootstrap, **kwargs)

    async def ensure_container(_tenant):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_process)
    monkeypatch.setattr(tool, "_ensure_container", ensure_container)
    monkeypatch.setattr(ShellTool, "_schedule_idle_reaper", classmethod(lambda cls: None))
    monkeypatch.setattr(ShellTool, "_global_slots", asyncio.Semaphore(1))
    for name in ("_tenant_locks", "_user_slots", "_last_used_by_tenant"):
        monkeypatch.setattr(ShellTool, name, {})
    for name in ("_active_tenants", "_active_owners", "_prepared_tenants"):
        monkeypatch.setattr(ShellTool, name, set())
    monkeypatch.setattr(ShellTool, "_tenants", {tenant.container_name: tenant})
    command = "# large source\n" * 20_000 + """cat
python3 - <<'PY'
import sys
sys.stdout.write('O' * 1_100_000 + 'END-OUT')
sys.stderr.write('E' * 1_100_000 + 'END-ERR')
PY
"""
    stdout, stderr, code = asyncio.run(tool._run_shell_command_inner(command, tenant))
    assert code == 0
    assert stdout == b"O" * 1_100_000 + b"END-OUT"
    assert stderr == b"E" * 1_100_000 + b"END-ERR"
    from pathlib import Path
    assert script_paths and all(not Path(path).exists() for path in script_paths)


def test_shell_fails_closed_before_docker_if_host_policy_is_missing(monkeypatch):
    calls = []
    monkeypatch.setattr("plugins.shell.impl.egress_policy_ready", lambda: False)

    async def run_docker(*args, **kwargs):
        calls.append(args)
        return (b"", b"", 0)

    monkeypatch.setattr(ShellTool, "_run_docker", classmethod(lambda cls, *a, **k: run_docker(*a, **k)))
    with pytest.raises(RuntimeError, match="host egress firewall"):
        asyncio.run(ShellTool(None)._runtime_ready())
    assert calls == []


def test_shell_fails_closed_when_docker_live_restore_is_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr("plugins.shell.impl.egress_policy_ready", lambda: True)
    monkeypatch.setattr("plugins.shell.impl.resource_pool_ready", lambda: True)

    async def run_docker(*args, **kwargs):
        calls.append(args)
        if "{{json .Runtimes}}" in args:
            return (
                b'{"runsc":{"runtimeArgs":["--platform=systrap","--network=sandbox"]}}',
                b"",
            ), 0
        return (b"true", b""), 0

    monkeypatch.setattr(
        ShellTool,
        "_run_docker",
        classmethod(lambda cls, *a, **k: run_docker(*a, **k)),
    )
    with pytest.raises(RuntimeError, match="live-restore must be disabled"):
        asyncio.run(ShellTool(None)._runtime_ready())
    assert len(calls) == 2


def test_restart_recovery_removes_only_this_controllers_sandbox_ids(monkeypatch):
    calls = []
    monkeypatch.setenv("MAXWELL_CONTROLLER_ID", "dev")

    async def run_docker(cls, *args, timeout=30):
        calls.append(args)
        if args[0] == "ps":
            return (
                b"a" * 64 + b" dev\n"
                + b"b" * 12 + b" dev\n"
                + b"c" * 12 + b" \n"
                + b"d" * 12 + b" prod\n"
                + b"not-a-container-id dev\n",
                b"",
            ), 0
        return (b"", b""), 0

    monkeypatch.setattr(ShellTool, "_run_docker", classmethod(run_docker))
    monkeypatch.setattr(ShellTool, "_recovery_complete", False)

    asyncio.run(ShellTool._recover_stale_sandboxes())

    assert calls[0] == (
        "ps", "-a", "--filter", "label=maxwell.shell.managed=true",
        "--format", '{{.ID}} {{.Label "maxwell.shell.controller"}}',
    )
    assert calls[1] == ("rm", "-f", "a" * 64, "b" * 12)
    assert ShellTool._recovery_complete is True


def test_restart_recovery_default_controller_adopts_unlabeled_sandboxes(monkeypatch):
    calls = []
    monkeypatch.delenv("MAXWELL_CONTROLLER_ID", raising=False)

    async def run_docker(cls, *args, timeout=30):
        calls.append(args)
        if args[0] == "ps":
            return (b"a" * 64 + b" \n" + b"b" * 12 + b" dev\n", b""), 0
        return (b"", b""), 0

    monkeypatch.setattr(ShellTool, "_run_docker", classmethod(run_docker))
    monkeypatch.setattr(ShellTool, "_recovery_complete", False)

    asyncio.run(ShellTool._recover_stale_sandboxes())

    assert calls[1] == ("rm", "-f", "a" * 64)
    assert ShellTool._recovery_complete is True


def test_sandbox_naming_and_labels_are_controller_scoped(monkeypatch):
    from plugins.shell.isolation import docker_run_args, shell_tenant

    class Message:
        author = type("Author", (), {"id": "7"})()
        guild = type("Guild", (), {"id": "9"})()
        channel = type("Channel", (), {"id": "11"})()

    monkeypatch.delenv("MAXWELL_CONTROLLER_ID", raising=False)
    default_name = shell_tenant(Message()).container_name
    assert "maxwell.shell.controller" not in " ".join(
        docker_run_args(container_name=default_name, image="maxwell-shell")
    )

    monkeypatch.setenv("MAXWELL_CONTROLLER_ID", "dev")
    dev_name = shell_tenant(Message()).container_name
    assert dev_name != default_name
    assert re.fullmatch(r"mwsh-[a-f0-9]{24}", dev_name)
    args = docker_run_args(container_name=dev_name, image="maxwell-shell")
    assert "maxwell.shell.controller=dev" in args


def test_missing_controller_label_renders_as_own_not_foreign(monkeypatch):
    tenant = ShellTenant("7", "guild:9", "mwsh-" + "a" * 24)
    calls = []

    async def run_docker(cls, *args, timeout=30):
        calls.append(args)
        if args[0] == "inspect":
            # Docker renders a missing label as "<no value>".
            return (b"true runsc v2 <no value>", b""), 0
        return (b"", b""), 0

    monkeypatch.delenv("MAXWELL_CONTROLLER_ID", raising=False)
    monkeypatch.setattr(ShellTool, "_run_docker", classmethod(run_docker))
    monkeypatch.setattr(ShellTool, "_prepared_tenants", {tenant.container_name})
    asyncio.run(ShellTool(None)._ensure_container_impl(tenant))
    assert not any(call[0] in {"rm", "run"} for call in calls)


def test_foreign_controller_label_is_never_reused_or_destroyed(monkeypatch):
    tenant = ShellTenant("7", "guild:9", "mwsh-" + "a" * 24)
    calls = []

    async def run_docker(cls, *args, timeout=30):
        calls.append(args)
        if args[0] == "inspect":
            return (b"true runsc v2 dev", b""), 0
        return (b"", b""), 0

    monkeypatch.delenv("MAXWELL_CONTROLLER_ID", raising=False)
    monkeypatch.setattr(ShellTool, "_run_docker", classmethod(run_docker))
    monkeypatch.setattr(ShellTool, "_prepared_tenants", {tenant.container_name})
    with pytest.raises(RuntimeError, match="collision"):
        asyncio.run(ShellTool(None)._ensure_container_impl(tenant))
    assert not any(call[0] in {"rm", "run"} for call in calls)


def test_command_timeout_destroys_the_tenant_container(monkeypatch):
    tenant = ShellTenant("7", "guild:9", "mwsh-" + "a" * 24)
    tool = ShellTool(None)
    destroyed = []
    killed_execs = []

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdout = None
            self.stderr = None
            self.stdin = type("Input", (), {
                "write": lambda self, data: None,
                "drain": lambda self: asyncio.sleep(0),
                "close": lambda self: None,
            })()
            self.killed = False

        async def wait(self):
            if self.killed:
                return -9
            await asyncio.Future()

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Process()

    async def create_process(*_args, **_kwargs):
        return process

    async def ensure_container(_tenant):
        return None

    async def kill_exec(_tenant, _pid_file):
        killed_execs.append(_tenant.container_name)

    async def destroy(cls, _tenant):
        destroyed.append(_tenant.container_name)
        cls._prepared_tenants.discard(_tenant.container_name)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(tool, "_ensure_container", ensure_container)
    monkeypatch.setattr(tool, "_timeout_seconds", lambda: 0.01)
    monkeypatch.setattr(tool, "_kill_container_exec", kill_exec)
    monkeypatch.setattr(ShellTool, "_destroy_container_unlocked", classmethod(destroy))
    monkeypatch.setattr(ShellTool, "_schedule_idle_reaper", classmethod(lambda cls: None))
    monkeypatch.setattr(ShellTool, "_global_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(ShellTool, "_tenant_locks", {})
    monkeypatch.setattr(ShellTool, "_user_slots", {})
    monkeypatch.setattr(ShellTool, "_active_tenants", set())
    monkeypatch.setattr(ShellTool, "_active_owners", set())
    monkeypatch.setattr(ShellTool, "_prepared_tenants", {tenant.container_name})
    monkeypatch.setattr(ShellTool, "_tenants", {tenant.container_name: tenant})
    monkeypatch.setattr(ShellTool, "_last_used_by_tenant", {})

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(tool._run_shell_command_inner("sleep 600", tenant))

    assert killed_execs == [tenant.container_name]
    assert destroyed == [tenant.container_name]
    assert process.killed is True
    assert tenant.container_name not in ShellTool._active_tenants
