"""GitHub project workspaces: policy isolation and ref safety."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from maxwell_core.tools.dispatch import _prepare_tool_params
from plugins.github_projects.impl import (
    ExecResult,
    GitHubProjectService,
    GitHubRepoTool,
    PolicyStore,
    authorize_url,
    normalize_scopes,
    sign_oauth_state,
    verify_oauth_state,
    _ref,
    _repo,
)


class Ctx:
    def __init__(self, root):
        self.data_dir = Path(root)


class Bot:
    config = SimpleNamespace(DATA_DIR="data")


def test_repo_requires_owner_name():
    assert _repo("Owner/name.git") == "Owner/name"
    for bad in ("", "justname", "a/b/c", "../etc/passwd", "owner/name space", "../app", "acme/..", "./app", "acme/."):
        with pytest.raises(ValueError):
            _repo(bad)


def test_ref_rejects_git_escape():
    assert _ref("main") == "main"
    assert _ref("feat/foo-bar") == "feat/foo-bar"
    for bad in ("..", "refs/../HEAD", "x@{0}", "/abs", ".hidden", "a//b", "x.lock"):
        with pytest.raises(ValueError):
            _ref(bad)


def test_policy_is_per_user(tmp_path):
    async def run():
        store = PolicyStore(tmp_path / "policies.json")
        await store.set("1", "acme/app", {"mode": "write"})
        await store.set("2", "acme/app", {"mode": "read"})
        assert (await store.get("1", "acme/app"))["mode"] == "write"
        assert (await store.get("2", "acme/app"))["mode"] == "read"
        assert await store.get("1", "other/repo") == {}

    asyncio.run(run())


def test_persisted_repository_agents_cannot_be_restored(tmp_path):
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({"1": {"acme/app": {
        "mode": "write", "auto_review": True, "schedule_enabled": True,
        "schedule_goal": "old task", "schedule_minutes": 5,
    }}}))

    async def run():
        store = PolicyStore(path)
        assert await store.get("1", "acme/app") == {"mode": "write"}
        saved = await store.set("1", "acme/app", {"auto_merge": True, "mode": "read"})
        assert set(saved) == {"mode", "updated_at"}
        assert "schedule_enabled" not in path.read_text()

    asyncio.run(run())


@pytest.mark.parametrize("kwargs", [
    {"action": "schedule_set", "schedule_goal": "old task"},
    {"action": "policy_set", "auto_review": True},
])
def test_repository_agent_controls_are_rejected(tmp_path, kwargs):
    service = GitHubProjectService(Bot(), Ctx(tmp_path))
    tool = GitHubRepoTool(Bot(), service)
    message = SimpleNamespace(author=SimpleNamespace(id=1))
    result = asyncio.run(tool.execute(message, repo="acme/app", **kwargs))
    assert result.startswith("Error:")
    assert "were removed" in result
    assert "schedule_set" not in tool.parameters["properties"]["action"]["enum"]
    assert "auto_review" not in tool.parameters["properties"]


def test_write_and_merge_require_policy_mode():
    GitHubProjectService.require_mode({"mode": "write"}, "write")
    GitHubProjectService.require_mode({"mode": "admin"}, "write")
    with pytest.raises(PermissionError):
        GitHubProjectService.require_mode({"mode": "read"}, "write")
    with pytest.raises(PermissionError):
        GitHubProjectService.require_mode({"mode": "write"}, "admin")


def test_workspaces_do_not_share_users(tmp_path):
    svc = GitHubProjectService(Bot(), Ctx(tmp_path))
    a = svc.user_root("1")
    b = svc.user_root("2")
    assert a != b
    assert a.parent == b.parent == svc.workspace_root
    repo = svc.repo_root("1", "acme/app")
    assert repo == a / "repos" / "acme" / "app"
    assert svc.user_root("1") in repo.parents

@pytest.mark.parametrize("operation", ["workspace", "inspection", "git"])
def test_docker_workflows_map_workspace_to_host_data_volume(tmp_path, monkeypatch, operation):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAXWELL_HOST_DATA_BIND", "/docker-host/maxwell-data")
    svc = GitHubProjectService(Bot(), Ctx(tmp_path / "plugins" / "github_projects"))
    root = svc.repo_root("1", "acme/app")
    root.mkdir()
    calls = []

    async def fake_proc(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "inspect"):
            return ExecResult(1, "", "no container")
        return ExecResult(0, "", "")

    monkeypatch.setattr(svc, "_ensure_image", AsyncMock())
    monkeypatch.setattr(svc, "_proc", fake_proc)
    monkeypatch.setattr(svc, "token", lambda *_args: "fake-test-token")
    if operation == "workspace":
        asyncio.run(svc.ensure_container("1"))
    elif operation == "inspection":
        asyncio.run(svc.inspect_repository("1", "acme/app", "status"))
    else:
        asyncio.run(svc._trusted_git(
            "1", "acme/app", {}, [["status", "--short", "--branch"]],
        ))
    run = next(args for args in calls if args[:2] == ("docker", "run"))
    mount = run[run.index("-v") + 1]
    expected = "/docker-host/maxwell-data/" + str(svc.user_root("1").relative_to(tmp_path))
    assert mount == expected + (":/workspace:ro" if operation == "inspection" else ":/workspace:rw")



def test_escaping_repo_symlink_cannot_create_host_directories(tmp_path):
    svc = GitHubProjectService(Bot(), Ctx(tmp_path / "plugin"))
    outside = tmp_path / "outside"
    (svc.user_root("1") / "repos").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="workspace escaped user root"):
        svc.repo_root("1", "acme/app")
    assert not outside.exists()


def test_user_workspace_symlink_cannot_adopt_another_users_files(tmp_path):
    svc = GitHubProjectService(Bot(), Ctx(tmp_path))
    first = svc.user_root("1")
    second = svc.user_root("2")
    first.rmdir()
    first.symlink_to(second, target_is_directory=True)
    with pytest.raises(ValueError, match="workspace escaped user root"):
        svc.user_root("1")


def test_list_without_user_token_returns_error(tmp_path, monkeypatch):
    monkeypatch.delenv("MAXWELL_GITHUB_USER_TOKEN_664824253526573056", raising=False)

    async def run():
        svc = GitHubProjectService(Bot(), Ctx(tmp_path))
        tool = GitHubRepoTool(Bot(), svc)
        msg = SimpleNamespace(
            author=SimpleNamespace(id="664824253526573056"),
            channel=SimpleNamespace(id="1"),
        )
        out = await tool.execute(msg, action="list")
        assert out.startswith("Error:")
        assert "github_repo action=auth" in out

    asyncio.run(run())


def test_auth_set_is_per_user_and_never_echoed(tmp_path, monkeypatch):
    monkeypatch.delenv("MAXWELL_GITHUB_USER_TOKEN_1", raising=False)
    monkeypatch.delenv("MAXWELL_GITHUB_USER_TOKEN_2", raising=False)
    pat = "ghp_" + ("a" * 36)

    async def run():
        svc = GitHubProjectService(Bot(), Ctx(tmp_path))
        tool = GitHubRepoTool(Bot(), svc)
        one = SimpleNamespace(author=SimpleNamespace(id="1"), channel=SimpleNamespace(id="10"))
        two = SimpleNamespace(author=SimpleNamespace(id="2"), channel=SimpleNamespace(id="20"))
        out = await tool.execute(one, action="auth_set", token=pat)
        assert "saved" in out.lower()
        assert pat not in out
        assert svc.token("1") == pat
        assert svc.token("2") == ""
        assert await tool.execute(two, action="auth_clear")
        assert svc.token("1") == pat
        await tool.execute(one, action="auth_clear")
        assert svc.token("1") == ""

    asyncio.run(run())


def test_oauth_scopes_and_signed_login_link(monkeypatch):
    monkeypatch.setenv("MAXWELL_GITHUB_OAUTH_CLIENT_ID", "client123")
    monkeypatch.setenv("MAXWELL_GITHUB_OAUTH_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://maxwell.z3ki.dev")
    assert normalize_scopes("repo,workflow") == ["repo", "workflow"]
    with pytest.raises(ValueError):
        normalize_scopes("repo,admin:org")
    state = sign_oauth_state("664824253526573056", ["repo"])
    payload = verify_oauth_state(state)
    assert payload["uid"] == "664824253526573056"
    assert payload["scopes"] == ["repo"]
    assert payload["issued_at"] > 0
    with pytest.raises(ValueError):
        verify_oauth_state(state[:-1] + ("0" if state[-1] != "0" else "1"))
    url = authorize_url("664824253526573056", "repo,workflow")
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=client123" in url
    assert "repo+workflow" in url or "repo%20workflow" in url


def test_auth_returns_login_link(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_GITHUB_OAUTH_CLIENT_ID", "client123")
    monkeypatch.setenv("MAXWELL_GITHUB_OAUTH_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://maxwell.z3ki.dev")
    monkeypatch.delenv("MAXWELL_GITHUB_USER_TOKEN_1", raising=False)

    async def run():
        svc = GitHubProjectService(Bot(), Ctx(tmp_path))
        tool = GitHubRepoTool(Bot(), svc)
        msg = SimpleNamespace(author=SimpleNamespace(id="1"), channel=SimpleNamespace(id="10"))
        out = await tool.execute(msg, action="auth", scopes="repo,workflow")
        assert "https://github.com/login/oauth/authorize?" in out
        assert "scopes=repo,workflow" in out
        assert "s3cret" not in out

    asyncio.run(run())


def test_dispatched_commit_preserves_text_and_rejects_blank(tmp_path, monkeypatch):
    """The model's commit text must become an actual commit, not a context kwarg."""
    svc = GitHubProjectService(Bot(), Ctx(tmp_path))
    root = svc.repo_root("1", "acme/app")
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (root / "file.txt").write_text("committed contents\n")
    text = "Fix 'quoted' text; $(touch injected)\n\nKeep the full commit body."

    async def local_git(uid, repo, policy, commands, *, timeout):
        # Exercise real Git; credential isolation has its own service coverage.
        for command in commands:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(root), *command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            result = ExecResult(proc.returncode, out.decode(), err.decode())
            if result.code:
                return result
        return result

    monkeypatch.setattr(svc, "git", local_git)

    async def scenario():
        await svc.policy.set("1", "acme/app", {"mode": "write"})
        tool = GitHubRepoTool(Bot(), svc)
        message = SimpleNamespace(author=SimpleNamespace(id="1"))
        params = _prepare_tool_params(
            "github_repo",
            {"action": "commit", "repo": "acme/app", "commit_message": text},
        )
        result = await tool.execute(message, **params)
        assert result.startswith("exit=0"), result
        committed = subprocess.check_output(
            ["git", "-C", str(root), "log", "-1", "--format=%B"], text=True,
        ).strip()
        assert committed == text
        assert subprocess.check_output(
            ["git", "-C", str(root), "show", "HEAD:file.txt"], text=True,
        ) == "committed contents\n"
        assert not (root / "injected").exists()
        before = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"])
        for missing in ({}, {"commit_message": " \n "}):
            result = await tool.execute(
                message, action="commit", repo="acme/app", **missing,
            )
            assert result.startswith("Error:")
            assert subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
            ) == before

    asyncio.run(scenario())


def test_repository_parent_creation_does_not_follow_concurrent_guest_rename(
    tmp_path, monkeypatch,
):
    import os

    svc = GitHubProjectService(Bot(), Ctx(tmp_path / "plugin"))
    user_root = svc.user_root("1")
    repos = user_root / "repos"
    repos.mkdir()
    renamed = user_root / "renamed-repos"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = os.mkdir
    raced = []

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if not raced and (
            (dir_fd is not None and path == "acme")
            or (dir_fd is None and Path(path) == repos / "acme")
        ):
            raced.append(True)
            repos.rename(renamed)
            repos.symlink_to(outside, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir)
    try:
        svc.repo_root("1", "acme/app")
    except ValueError:
        # An escape rejection must happen before any host-side creation.
        pass
    assert raced
    assert not (outside / "acme").exists()
    assert (renamed / "acme").is_dir()
