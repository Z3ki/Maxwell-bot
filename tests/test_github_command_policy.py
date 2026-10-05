"""Repository code execution requires an actual, scoped, stored opt-in."""

import asyncio
from types import SimpleNamespace

import pytest

from plugins.github_projects.impl import (
    ExecResult,
    GitHubProjectService,
    GitHubRepoTool,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    bot = SimpleNamespace(bg_jobs=None, config=SimpleNamespace(DATA_DIR=str(tmp_path)))
    service = GitHubProjectService(bot, SimpleNamespace(data_dir=tmp_path))
    service.repo_root("1", "acme/app").mkdir()
    calls = []

    async def process(*args, **kwargs):
        calls.append((args, kwargs))
        return ExecResult(0, "simulated output\n", "")

    async def container(_uid):
        return "simulated-container"

    monkeypatch.setattr(service, "_proc", process)
    monkeypatch.setattr(service, "ensure_container", container)
    return service, GitHubRepoTool(bot, service), calls


def _message(uid="1"):
    return SimpleNamespace(
        author=SimpleNamespace(id=uid), channel=SimpleNamespace(id="10")
    )


@pytest.mark.parametrize("action", ["run", "verify"])
@pytest.mark.parametrize(
    "command",
    [
        "nmap",
        "nmap\t--version",
        "nmap\n--version",
        "'nmap' --version",
        '"hydra" -h',
        "$'nmap' --version",
        'cmd=nma; "${cmd}p" --version',
        "$(printf nmap) --version",
        "python -c 'import subprocess; subprocess.run([\"nmap\"])'",
        "bash -lc 'nmap --version'",
        "python -m pytest",
    ],
)
def test_disabled_policy_refuses_custom_commands_before_any_process(
    workspace, action, command
):
    service, tool, calls = workspace

    async def scenario():
        await service.policy.set("1", "acme/app", {"mode": "read"})
        result = await tool.execute(
            _message(), action=action, repo="acme/app", command=command
        )
        assert result.startswith("Error:")
        assert "allow_security_testing=true" in result
        assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, False, 0, 1, "true", "false", [], {}])
def test_direct_service_requires_strict_boolean_opt_in(workspace, value):
    service, _, calls = workspace

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": value})
        with pytest.raises(PermissionError, match="repo policy"):
            await service.run("1", "acme/app", "echo allowed?")
        assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["run", "verify"])
def test_per_call_flag_cannot_override_stored_policy(workspace, action):
    service, tool, calls = workspace

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": False})
        result = await tool.execute(
            _message(),
            action=action,
            repo="acme/app",
            command="echo test",
            allow_security_testing=True,
        )
        assert "repo policy" in result
        assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("uid,repo", [("2", "acme/app"), ("1", "acme/other")])
def test_opt_in_does_not_grant_other_users_or_repositories(workspace, uid, repo):
    service, _, calls = workspace

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": True})
        with pytest.raises(PermissionError, match="repo policy"):
            await service.run(uid, repo, "echo test")
        assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["run", "verify"])
def test_opt_in_preserves_custom_command_text_and_execution(workspace, action):
    service, tool, calls = workspace
    command = "printf 'ordinary test'; python -m pytest"

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": True})
        result = await tool.execute(
            _message(), action=action, repo="acme/app", command=command
        )
        assert not result.startswith("Error:")
        executed = [args for args, _ in calls if args[:2] == ("docker", "exec")]
        assert len(executed) == 1
        assert executed[0][-3:] == ("bash", "-lc", command)

    asyncio.run(scenario())


def test_policy_revoked_during_verification_blocks_custom_execution(
    workspace, monkeypatch
):
    service, _, calls = workspace
    inspections = []

    async def inspect(uid, repo, operation, **kwargs):
        inspections.append(operation)
        await service.policy.set(uid, repo, {"allow_security_testing": False})
        return ExecResult(0, "inspection completed", "")

    monkeypatch.setattr(service, "inspect_repository", inspect)

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": True})
        with pytest.raises(PermissionError, match="repo policy"):
            await service.verify("1", "acme/app", "echo test")
        assert inspections == ["verify"]
        assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["status", "diff", "verify"])
def test_read_only_inspection_uses_isolated_container_without_opt_in(workspace, action):
    service, tool, calls = workspace

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": False})
        result = await tool.execute(_message(), action=action, repo="acme/app")
        assert not result.startswith("Error:")
        assert not any(args[:2] == ("docker", "exec") for args, _ in calls)
        sidecars = [args for args, _ in calls if args[:2] == ("docker", "run")]
        assert len(sidecars) == 1
        args = sidecars[0]
        assert "--read-only" in args
        assert args[args.index("--network") + 1] == "none"
        assert (
            args[args.index("-v") + 1]
            == f"{service.repo_root('1', 'acme/app')}:/repository:ro"
        )
        assert args[args.index("--workdir") + 1] == "/tmp"
        assert args[args.index("python3") + 1 : args.index("python3") + 3] == (
            "-I",
            "-c",
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["git", "checkout", "checkout_pr"])
def test_git_operations_cannot_execute_repository_helpers_without_opt_in(
    workspace, operation
):
    service, _, calls = workspace

    async def scenario():
        policy = {"allow_security_testing": True, "mode": "admin"}
        with pytest.raises(PermissionError, match="repo policy"):
            if operation == "git":
                await service.git("1", "acme/app", policy, [["status", "--short", "--branch"]])
            elif operation == "checkout":
                await service.checkout("1", "acme/app", policy)
            else:
                await service.checkout_pr("1", "acme/app", policy, 1)
        assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ref", ["--ext-diff", "--textconv", "--all", "--recurse-submodules", "-main"]
)
def test_diff_ref_cannot_inject_git_options(workspace, ref):
    service, tool, calls = workspace

    async def scenario():
        await service.policy.set("1", "acme/app", {"mode": "read"})
        result = await tool.execute(_message(), action="diff", repo="acme/app", ref=ref)
        assert "invalid git ref" in result
        assert not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [1, "true", "false", [], {}])
def test_policy_set_rejects_non_boolean_grants_without_api_calls(workspace, value):
    _, tool, calls = workspace

    async def scenario():
        result = await tool.execute(
            _message(),
            action="policy_set",
            repo="acme/app",
            allow_security_testing=value,
        )
        assert "must be a boolean" in result
        assert not calls

    asyncio.run(scenario())


def test_unknown_inspection_operation_is_rejected_before_process(workspace):
    service, _, calls = workspace
    with pytest.raises(ValueError, match="unsupported repository inspection"):
        asyncio.run(service.inspect_repository("1", "acme/app", "run"))
    assert not calls


def test_missing_checkout_cannot_start_inspection_sidecar(workspace):
    service, _, calls = workspace
    with pytest.raises(FileNotFoundError):
        asyncio.run(service.inspect_repository("1", "acme/missing", "status"))
    assert not calls
