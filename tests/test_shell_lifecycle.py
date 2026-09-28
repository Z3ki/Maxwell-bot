"""Fail-closed startup, restart recovery, and timeout cleanup for shell guests."""

from __future__ import annotations

import asyncio

import pytest

from plugins.shell.impl import ShellTool
from plugins.shell.isolation import ShellTenant


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


def test_restart_recovery_removes_only_labeled_sandbox_ids(monkeypatch):
    calls = []

    async def run_docker(cls, *args, timeout=30):
        calls.append(args)
        if args[0] == "ps":
            return (b"a" * 64 + b"\nnot-a-container-id\n" + b"b" * 12 + b"\n", b""), 0
        return (b"", b""), 0

    monkeypatch.setattr(ShellTool, "_run_docker", classmethod(run_docker))
    monkeypatch.setattr(ShellTool, "_recovery_complete", False)

    asyncio.run(ShellTool._recover_stale_sandboxes())

    assert calls[0][:2] == ("ps", "-aq")
    assert calls[1] == ("rm", "-f", "a" * 64, "b" * 12)
    assert ShellTool._recovery_complete is True


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
