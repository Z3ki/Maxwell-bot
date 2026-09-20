"""GitHub project workspaces: policy isolation, ref safety, signed webhooks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.api_server as api
from plugins.github_projects.impl import (
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
    bg_jobs = None
    config = SimpleNamespace(DATA_DIR="data")


def test_repo_requires_owner_name():
    assert _repo("Owner/name.git") == "Owner/name"
    for bad in ("", "justname", "a/b/c", "../etc/passwd", "owner/name space"):
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


class _WebhookRequest:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self._raw = body
        self.headers = headers
        self.path = "/api/github/webhook"
        self.method = "POST"

    async def read(self):
        return self._raw


def _payload(resp):
    return json.loads(resp.text)


def test_github_webhook_requires_secret_and_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    monkeypatch.delenv("MAXWELL_GITHUB_WEBHOOK_SECRET", raising=False)
    body = b'{"repository":{"full_name":"acme/app"},"number":3,"action":"opened"}'
    req = _WebhookRequest(body, {"X-GitHub-Event": "pull_request"})
    missing = asyncio.run(api.github_webhook(req))
    assert missing.status == 503

    monkeypatch.setenv("MAXWELL_GITHUB_WEBHOOK_SECRET", "s3cret")
    bad = asyncio.run(api.github_webhook(req))
    assert bad.status == 401

    digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    good = asyncio.run(
        api.github_webhook(
            _WebhookRequest(
                body,
                {
                    "X-Hub-Signature-256": "sha256=" + digest,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "abc",
                },
            )
        )
    )
    assert good.status == 200
    assert _payload(good) == {"ok": True}
    queued = json.loads(
        (tmp_path / "plugins" / "github_projects" / "webhook_events.json").read_text()
    )
    assert queued[-1]["repo"] == "acme/app"
    assert queued[-1]["number"] == 3
    assert queued[-1]["event"] == "pull_request"


def test_github_webhook_ignores_unhandled_events(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MAXWELL_GITHUB_WEBHOOK_SECRET", "s3cret")
    body = b'{"repository":{"full_name":"acme/app"},"action":"created"}'
    digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    resp = asyncio.run(
        api.github_webhook(
            _WebhookRequest(
                body,
                {
                    "X-Hub-Signature-256": "sha256=" + digest,
                    "X-GitHub-Event": "star",
                },
            )
        )
    )
    assert _payload(resp) == {"ok": True, "ignored": "star"}
    queue = tmp_path / "plugins" / "github_projects" / "webhook_events.json"
    assert not queue.exists()


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
