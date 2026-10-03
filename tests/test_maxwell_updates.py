import asyncio
import base64
import json
from types import SimpleNamespace

from plugins.maxwell_extras import updates


def test_updates_keep_running_revision_separate_from_main(tmp_path, monkeypatch):
    old, new = "a" * 40, "b" * 40
    monkeypatch.setattr(updates, "_running_snapshot", lambda root: {"commit": old, "changelog": "running changes", "source": "startup_checkout"})
    tool = updates.MaxwellUpdatesTool(SimpleNamespace(), root=tmp_path)
    schema = tool.get_parameters()
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"limit", "commit", "local_only", "include_diff"}
    called = []

    async def fetch(session, path, params=None):
        called.append((path, params))
        if path == "/commits":
            return [{"sha": new, "commit": {"message": "new feature", "committer": {"date": "2026-10-03"}}}]
        if path == f"/commits/{old}":
            return {"sha": old, "commit": {"message": "old version"}, "files": [{"filename": "bot.py", "status": "modified", "additions": 5, "patch": "+ real change"}]}
        assert params == {"ref": new}
        return {"encoding": "base64", "content": base64.b64encode(b"Unreleased changes").decode()}

    monkeypatch.setattr(tool, "_fetch_json", fetch)
    result = json.loads(asyncio.run(tool.execute(SimpleNamespace(), commit=old, include_diff=True)))
    assert result["running"]["commit"] == old
    assert result["main_commit"] == new
    assert not result["running_commit_matches_main"]
    assert result["selected_commit"]["files"][0]["filename"] == "bot.py"
    assert result["selected_commit"]["files"][0]["patch"] == "+ real change"
    assert result["main_changelog"] == "Unreleased changes"
    assert "not be deployed" in result["note"]


def test_updates_fail_gracefully_and_respect_web_off(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_running_snapshot", lambda root: {"commit": None, "changelog": "local evidence"})
    tool = updates.MaxwellUpdatesTool(SimpleNamespace(), root=tmp_path)
    calls = []

    async def fail(*args):
        calls.append(1)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(tool, "_fetch_json", fail)
    result = json.loads(asyncio.run(tool.execute(SimpleNamespace())))
    assert "Unavailable" in result["github_status"]
    assert result["running"]["changelog"] == "local evidence"
    result = json.loads(asyncio.run(tool.execute(SimpleNamespace(user_install_web_mode="off"))))
    assert "Not fetched" in result["github_status"]
    assert len(calls) == 1
    assert "error" in json.loads(asyncio.run(tool.execute(SimpleNamespace(), commit="../../secrets")))
    assert len(calls) == 1


def test_runtime_snapshot_falls_back_to_image_revision_without_git(tmp_path, monkeypatch):
    sha = "a" * 40
    monkeypatch.setenv("MAXWELL_BUILD_COMMIT", sha)
    monkeypatch.setattr(updates.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    (tmp_path / "CHANGELOG.md").write_text("Installed release", encoding="utf-8")
    result = updates._running_snapshot(tmp_path)
    assert result["commit"] == sha
    assert result["source"] == "image_build"
    assert result["changelog"] == "Installed release"


def test_github_results_are_cached_and_redirects_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_running_snapshot", lambda root: {"commit": None})
    tool = updates.MaxwellUpdatesTool(SimpleNamespace(), root=tmp_path)
    calls = []

    class Content:
        async def iter_chunked(self, size):
            yield b'{"value":1}'

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def get(self, url, **kwargs):
            assert url.startswith("https://api.github.com/repos/Z3ki/Maxwell-bot/")
            assert kwargs["allow_redirects"] is False
            calls.append(url)
            return Response()

    async def run():
        assert await tool._fetch_json(Session(), "/commits") == {"value": 1}
        assert await tool._fetch_json(Session(), "/commits") == {"value": 1}

    asyncio.run(run())
    assert len(calls) == 1
