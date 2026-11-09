import asyncio
from types import SimpleNamespace

import pytest

from plugins.github_projects.impl import ExecResult, GitHubProjectService


def test_proc_timeout_invokes_guest_cleanup(monkeypatch):
    cleaned = []
    killed = []

    class Proc:
        returncode = None

        async def communicate(self, input=None):
            if killed:
                return b"", b""
            await asyncio.sleep(10)

        def kill(self):
            killed.append(True)

        async def wait(self):
            return None

    async def create(*_args, **_kwargs):
        return Proc()

    async def cleanup():
        cleaned.append(True)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def run():
        try:
            await GitHubProjectService._proc(
                SimpleNamespace(),
                "docker",
                "exec",
                "x",
                timeout=0.01,
                on_timeout=cleanup,
            )
        except RuntimeError as exc:
            assert "timed out" in str(exc)
        else:
            raise AssertionError("timeout did not raise")

    asyncio.run(run())
    assert cleaned == [True]
    assert killed == [True]


def test_cancelling_proc_cleans_guest_and_reaps_client(monkeypatch):
    events = []

    class Proc:
        async def communicate(self, input=None):
            if "kill" in events:
                events.append("drain")
                return b"", b""
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            events.append("kill")

        async def wait(self):
            events.append("wait")

    async def create(*_args, **_kwargs):
        return Proc()

    async def cleanup():
        events.append("guest cleanup")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def scenario():
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(
            GitHubProjectService._proc(
                SimpleNamespace(),
                "docker",
                "exec",
                "fixture",
                on_timeout=cleanup,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = None
    asyncio.run(scenario())
    assert events == ["guest cleanup", "kill", "drain", "wait"]


def test_timeout_racing_client_exit_preserves_timeout_error(monkeypatch):
    class Proc:
        async def communicate(self, input=None):
            raise asyncio.TimeoutError

        def kill(self):
            raise ProcessLookupError

        async def wait(self):
            return 0

    async def create(*_args, **_kwargs):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(RuntimeError, match="command timed out"):
        asyncio.run(GitHubProjectService._proc(SimpleNamespace(), "fixture"))


@pytest.mark.parametrize("operation", ["inspect", "git"])
@pytest.mark.parametrize("interrupt", ["timeout", "cancel"])
def test_interrupted_sidecar_removes_only_its_unique_container(
    tmp_path,
    monkeypatch,
    operation,
    interrupt,
):
    service = GitHubProjectService(
        SimpleNamespace(), SimpleNamespace(data_dir=tmp_path)
    )
    service.repo_root("1", "acme/app").mkdir()
    calls = []
    events = []

    class Proc:
        returncode = 0

        def __init__(self, args):
            self.args = args

        async def communicate(self, input=None):
            if getattr(self, "killed", False):
                return b"", b""
            if self.args[:2] == ("docker", "run"):
                if interrupt == "timeout":
                    raise asyncio.TimeoutError
                started.set()
                await asyncio.Event().wait()
            return b"", b""

        def kill(self):
            self.killed = True
            events.append("kill")

        async def wait(self):
            events.append("wait")

    async def create(*args, **kwargs):
        calls.append(args)
        return Proc(args)

    async def image():
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(service, "_ensure_image", image)
    monkeypatch.setattr(service, "token", lambda *_args: "synthetic-sidecar-token")

    async def scenario():
        nonlocal started
        started = asyncio.Event()
        if operation == "inspect":
            pending = service.inspect_repository("1", "acme/app", "status")
        else:
            pending = service._trusted_git(
                "1",
                "acme/app",
                {},
                [["status", "--short", "--branch"]],
            )
        task = asyncio.create_task(pending)
        if interrupt == "cancel":
            await started.wait()
            task.cancel()
        expected = asyncio.CancelledError if interrupt == "cancel" else RuntimeError
        with pytest.raises(expected):
            await task

    started = None
    asyncio.run(scenario())
    run, remove = calls
    name = run[run.index("--name") + 1]
    assert name.startswith(service._container("1") + "-" + operation + "-")
    assert name != service._container("1")
    assert remove == ("docker", "rm", "-f", name)
    assert events == ["kill", "wait"]


def test_workspace_timeout_kills_the_captured_container(tmp_path, monkeypatch):
    service = GitHubProjectService(
        SimpleNamespace(), SimpleNamespace(data_dir=tmp_path)
    )
    service.repo_root("1", "acme/app").mkdir()
    calls = []
    killed = []

    async def container(_uid):
        return "synthetic-workspace"

    async def process(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("docker", "inspect", "-f"):
            return ExecResult(0, "container-id-abc\n", "")
        if "on_timeout" in kwargs:
            await kwargs["on_timeout"]()
        return ExecResult(0, "", "")

    async def cleanup(container_id):
        killed.append(container_id)

    monkeypatch.setattr(service, "ensure_container", container)
    monkeypatch.setattr(service, "_proc", process)
    monkeypatch.setattr(service, "_kill_workspace_exec", cleanup)

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": True})
        await service.run("1", "acme/app", "sleep 300")

    asyncio.run(scenario())
    assert killed == ["container-id-abc"]
    assert calls[1][-4:] == ("container-id-abc", "bash", "-lc", "sleep 300")


def test_workspace_exec_does_not_fall_back_to_container_name(tmp_path, monkeypatch):
    service = GitHubProjectService(
        SimpleNamespace(), SimpleNamespace(data_dir=tmp_path)
    )
    service.repo_root("1", "acme/app").mkdir()
    calls = []

    async def container(_uid):
        return "synthetic-workspace"

    async def process(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("docker", "inspect", "-f"):
            return ExecResult(1, "", "no such container")
        return ExecResult(0, "", "")

    monkeypatch.setattr(service, "ensure_container", container)
    monkeypatch.setattr(service, "_proc", process)

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": True})
        await service.run("1", "acme/app", "sleep 300")

    with pytest.raises(RuntimeError, match="could not identify user workspace"):
        asyncio.run(scenario())
    assert all(args[:2] != ("docker", "exec") for args in calls)

