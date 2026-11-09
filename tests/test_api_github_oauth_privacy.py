"""OAuth completion must not restore a credential erased after link issuance."""

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

import api.api_server as api
import plugins.github_projects.impl as github


@pytest.mark.parametrize("issued_at,expected_status", [(100.0, 503), (201.0, 200)])
def test_github_callback_stages_login_until_discord_confirmation(tmp_path, monkeypatch, issued_at, expected_status):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    monkeypatch.setattr("config.Config.DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAXWELL_GITHUB_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("MAXWELL_GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setattr(github.time, "time", lambda: issued_at)
    state = github.sign_oauth_state("1")
    monkeypatch.setattr(github.time, "time", lambda: 300.0)
    preferences = tmp_path / "plugins/maxwell_extras/user_preferences.json"
    preferences.parent.mkdir(parents=True)
    preferences.write_text(json.dumps({"users": {"1": {"deleted_at": 200.0}}}))
    delivered = []

    class User:
        async def send(self, content):
            delivered.append(content)
            return object()

    class Bot:
        async def fetch_user(self, uid):
            assert uid == 1
            return User()

    class Response:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, **kwargs):
            return self.value

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            assert url == "https://github.com/login/oauth/access_token"
            return Response({"access_token": "synthetic-token"})

        def get(self, url, **kwargs):
            assert url == "https://api.github.com/user"
            return Response({"login": "test-login"})

    monkeypatch.setattr(api.aiohttp, "ClientSession", Session)
    request = SimpleNamespace(
        query={"code": "test-code", "state": state},
        app={"bot": Bot()},
    )
    response = asyncio.run(api.github_oauth_callback(request))
    assert response.status == expected_status
    store = github.TokenStore(tmp_path / "plugins/github_projects/user_tokens.json")
    assert store.peek("1") == ""
    if expected_status != 200:
        assert delivered == []
        return
    code = re.search(r"\b\d{6}\b", delivered[0]).group(0)
    assert github.confirm_oauth("1", code) == "test-login"
    assert store.peek("1") == "synthetic-token"
