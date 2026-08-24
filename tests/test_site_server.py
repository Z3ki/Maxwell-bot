"""Site backend servers: input validation, isolation, lifecycle bookkeeping.

The parts that talk to docker are exercised against a fake `docker` so the
suite stays hermetic; what is checked here is everything that decides WHAT
docker gets asked to do — because that is where a path escape, a leaked
secret, or a cross-site reach would come from.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

import site_server
from bot_tools import SiteServerTool


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


# ── path and input validation ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "..", ".env", "_data/app.db", "a/b/c/d/e.py", "app.sh", "x" * 70 + ".py"],
)
def test_server_file_paths_are_refused(bad):
    with pytest.raises(site_server.SiteServerError):
        site_server.parse_files({bad: "print(1)"})


def test_server_files_accept_both_shapes():
    a = site_server.parse_files({"app.py": "x", "helpers.py": "y"})
    b = site_server.parse_files([{"path": "app.py", "content": "x"}])
    c = site_server.parse_files(json.dumps({"app.py": "x"}))
    assert set(a) == {"app.py", "helpers.py"}
    assert b == {"app.py": "x"} == c


def test_server_code_size_is_capped():
    with pytest.raises(site_server.SiteServerError, match="too large"):
        site_server.parse_files({"app.py": "#" * (site_server.MAX_CODE_BYTES + 1)})
    with pytest.raises(site_server.SiteServerError, match="too many"):
        site_server.parse_files({f"m{i}.py": "x" for i in range(site_server.MAX_FILES + 1)})


@pytest.mark.parametrize("bad", ["lower", "1START", "HAS-DASH", "WITH SPACE", ""])
def test_env_names_must_be_upper_snake(bad):
    with pytest.raises(site_server.SiteServerError):
        site_server.parse_env({bad: "v"})


@pytest.mark.parametrize("reserved", sorted(site_server.RESERVED_ENV))
def test_runtime_env_cannot_be_overridden(reserved):
    with pytest.raises(site_server.SiteServerError, match="runtime"):
        site_server.parse_env({reserved: "hijack"})


def test_env_values_are_capped_and_counted():
    with pytest.raises(site_server.SiteServerError, match="too long"):
        site_server.parse_env({"K": "x" * (site_server.MAX_ENV_VALUE + 1)})
    with pytest.raises(site_server.SiteServerError, match="too many"):
        site_server.parse_env({f"K{i}": "v" for i in range(site_server.MAX_ENV_KEYS + 1)})


# ── code on disk ──────────────────────────────────────────────────────────
def test_code_lives_outside_the_web_root(data_dir):
    """Source and secrets must never be reachable as a static file."""
    site_server.write_code(data_dir, "demo", {"app.py": "print(1)"})
    written = site_server.code_dir(data_dir, "demo") / "app.py"
    assert written.is_file()
    assert "site_servers" in str(written)
    assert "public" not in str(written) and "www" not in str(written)


def test_rewriting_replaces_source_but_keeps_the_database(data_dir):
    site_server.write_code(data_dir, "demo", {"app.py": "v1", "old.py": "gone"})
    db = site_server.state_dir(data_dir, "demo") / "app.db"
    db.write_text("rows")

    site_server.write_code(data_dir, "demo", {"app.py": "v2"})
    assert site_server.read_code(data_dir, "demo", "app.py") == "v2"
    assert [n for n, _ in site_server.list_code(data_dir, "demo")] == ["app.py"]
    assert db.read_text() == "rows"  # /data survived the redeploy


def test_read_cannot_escape_the_server_directory(data_dir, tmp_path):
    site_server.write_code(data_dir, "demo", {"app.py": "x"})
    (tmp_path / "secret.txt").write_text("token")
    for bad in ("../secret.txt", "../../secret.txt", "/etc/passwd"):
        with pytest.raises(site_server.SiteServerError):
            site_server.read_code(data_dir, "demo", bad)


def test_bad_slugs_are_refused_everywhere(data_dir):
    for bad in ("../evil", "UPPER", "a", ""):
        with pytest.raises(site_server.SiteServerError):
            site_server.code_dir(data_dir, bad)
        with pytest.raises(site_server.SiteServerError):
            site_server.container_name(bad)


# ── registry / proxy target ───────────────────────────────────────────────
def test_proxy_target_only_exists_while_running(data_dir):
    assert site_server.port_for(data_dir, "demo") is None
    site_server._write_entry(data_dir, "demo", {"port": 8801, "running": True})
    assert site_server.port_for(data_dir, "demo") == 8801
    site_server._write_entry(data_dir, "demo", {"port": 8801, "running": False})
    assert site_server.port_for(data_dir, "demo") is None


def test_ports_are_not_handed_out_twice(data_dir):
    site_server._write_entry(data_dir, "one", {"port": min(site_server.PORT_RANGE), "running": True})
    assert site_server._free_port(data_dir, "two") != min(site_server.PORT_RANGE)


def test_destroy_removes_code_and_registry(data_dir, monkeypatch):
    calls = []

    async def fake_docker(*args, **kw):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    site_server.write_code(data_dir, "demo", {"app.py": "x"})
    site_server._write_entry(data_dir, "demo", {"port": 8801, "running": True})

    run(site_server.destroy(data_dir, "demo"))
    assert not site_server.code_dir(data_dir, "demo").exists()
    assert site_server.get_entry(data_dir, "demo") is None
    assert any("rm" in a for a in calls)


# ── the tool ──────────────────────────────────────────────────────────────
@pytest.fixture
def tool(tmp_path, monkeypatch):
    control = {}
    bot = SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_SITE_DIR=str(tmp_path / "sites"),
            MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.com",
            DATA_DIR=str(tmp_path),
        ),
        _sites={"demo": {"user_id": "42", "title": "Demo"}},
        _load_sites=lambda quiet=True: None,
        _is_admin=lambda _uid: False,
        _control=control,
        control=control,
        tools={},
    )
    started = []

    async def fake_start(dd, slug, *, env=None):
        started.append((slug, env))
        site_server._write_entry(dd, slug, {"port": 8888, "running": True, "env": env or {}})
        return {"port": 8888}

    monkeypatch.setattr(site_server, "start", fake_start)
    t = SiteServerTool(bot)
    t._started = started
    return t


def _msg(uid=42):
    return SimpleNamespace(author=SimpleNamespace(id=uid, display_name="tester"))


def test_write_requires_an_app_py(tool):
    out = run(tool.execute(_msg(), name="demo", action="write", files={"server.py": "x"}))
    assert "must be called app.py" in out


def test_write_deploys_and_flags_the_site(tool):
    out = run(
        tool.execute(
            _msg(), name="demo", action="write",
            files={"app.py": "x"}, env={"API_KEY": "sekrit"},
        )
    )
    assert "Backend server live" in out
    assert "/bot/demo/api/" in out
    assert tool.bot._sites["demo"]["server"] is True
    assert tool._started == [("demo", {"API_KEY": "sekrit"})]


def test_secret_values_are_never_echoed_back(tool):
    run(tool.execute(_msg(), name="demo", action="write",
                     files={"app.py": "x"}, env={"API_KEY": "sk-do-not-leak"}))
    listed = run(tool.execute(_msg(), name="demo", action="env"))
    assert "API_KEY" in listed
    assert "sk-do-not-leak" not in listed
    status = run(tool.execute(_msg(), name="demo", action="status"))
    assert "sk-do-not-leak" not in status


def test_another_user_cannot_touch_the_backend(tool):
    for action in ("write", "logs", "status", "env", "delete", "start"):
        out = run(tool.execute(_msg(uid=999), name="demo", action=action, files={"app.py": "x"}))
        assert "belongs to someone else" in out
    assert tool._started == []


def test_unknown_action_lists_the_real_ones(tool):
    out = run(tool.execute(_msg(), name="demo", action="yolo"))
    assert "unknown action" in out
    assert "logs" in out and "write" in out


def test_errors_come_back_as_text_not_exceptions(tool):
    out = run(tool.execute(_msg(), name="demo", action="write", files={"../esc.py": "x"}))
    assert out.startswith("Error:")
    assert "unsafe" in out
