"""Site backend servers: input validation, isolation, lifecycle bookkeeping.

The parts that talk to docker are exercised against a fake `docker` so the
suite stays hermetic; what is checked here is everything that decides WHAT
docker gets asked to do — because that is where a path escape, a leaked
secret, or a cross-site reach would come from.
"""

import asyncio
import json
from pathlib import Path
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


def test_merge_code_overwrites_given_files_and_keeps_the_rest(data_dir):
    site_server.write_code(
        data_dir, "demo", {"app.py": "v1", "helpers.py": "keep", "old.py": "x"}
    )
    written = site_server.merge_code(data_dir, "demo", {"app.py": "v2"})
    assert written == ["app.py"]
    assert site_server.read_code(data_dir, "demo", "app.py") == "v2"
    assert site_server.read_code(data_dir, "demo", "helpers.py") == "keep"
    names = [n for n, _ in site_server.list_code(data_dir, "demo")]
    assert names == ["app.py", "helpers.py", "old.py"]


def test_patch_code_swaps_exact_text(data_dir):
    site_server.write_code(data_dir, "demo", {"app.py": "alpha beta alpha"})
    one = site_server.patch_code(data_dir, "demo", "app.py", "alpha", "gamma")
    assert "Patched app.py" in one
    assert site_server.read_code(data_dir, "demo", "app.py") == "gamma beta alpha"
    all_hits = site_server.patch_code(
        data_dir, "demo", "app.py", "a", "A", all_hits=True
    )
    assert "5 occurrence" in all_hits
    assert site_server.read_code(data_dir, "demo", "app.py") == "gAmmA betA AlphA"


def test_delete_code_file_refuses_app_py(data_dir):
    site_server.write_code(data_dir, "demo", {"app.py": "x", "helpers.py": "y"})
    assert site_server.delete_code_file(data_dir, "demo", "helpers.py") == "Deleted helpers.py"
    with pytest.raises(site_server.SiteServerError, match="app.py"):
        site_server.delete_code_file(data_dir, "demo", "app.py")
    assert [n for n, _ in site_server.list_code(data_dir, "demo")] == ["app.py"]


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


def test_corrupt_registry_entries_cannot_crash_port_lookup(data_dir):
    site_server.registry_path(data_dir).write_text(
        json.dumps(
            {
                "../escape": {"port": 8801, "running": True},
                "demo": {"port": "not-a-port", "running": "true"},
            }
        ),
        encoding="utf-8",
    )
    assert site_server.port_for(data_dir, "demo") is None
    port = site_server._free_port(data_dir, "other")
    assert port in site_server.PORT_RANGE
    assert site_server._port_is_free(port)


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

    async def fake_start(dd, slug, *, env=None, packages=None):
        started.append((slug, env, packages))
        site_server._write_entry(
            dd, slug,
            {"port": 8888, "running": True, "env": env or {}, "packages": packages or []},
        )
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
    assert tool._started == [("demo", {"API_KEY": "sekrit"}, None)]


def test_write_merges_helpers_instead_of_wiping_them(tool):
    run(
        tool.execute(
            _msg(),
            name="demo",
            action="write",
            files={"app.py": "from helpers import x", "helpers.py": "x = 1"},
        )
    )
    out = run(
        tool.execute(
            _msg(), name="demo", action="write", files={"app.py": "from helpers import x\n# patched"}
        )
    )
    assert "other source files kept" in out
    assert site_server.read_code(tool.bot.config.DATA_DIR, "demo", "helpers.py") == "x = 1"
    assert "# patched" in site_server.read_code(tool.bot.config.DATA_DIR, "demo", "app.py")


def test_secret_values_are_never_echoed_back(tool):
    run(tool.execute(_msg(), name="demo", action="write",
                     files={"app.py": "x"}, env={"API_KEY": "sk-do-not-leak"}))
    listed = run(tool.execute(_msg(), name="demo", action="env"))
    assert "API_KEY" in listed
    assert "sk-do-not-leak" not in listed
    status = run(tool.execute(_msg(), name="demo", action="status"))
    assert "sk-do-not-leak" not in status


def test_another_user_can_edit_the_backend(tool):
    out = run(
        tool.execute(
            _msg(uid=999),
            name="demo",
            action="write",
            files={"app.py": "print('ok')"},
        )
    )
    assert "belongs to someone else" not in out
    assert "Backend server live" in out
    assert tool._started


def test_server_read_windows_a_huge_file_and_refuses_a_repeat(tool):
    huge = "print(1)\n" + ("#x\n" * 12_000)
    msg = _msg()
    run(tool.execute(msg, name="demo", action="write", files={"app.py": huge}))
    out = run(tool.execute(msg, name="demo", action="read"))
    assert huge not in out
    assert "start_line" in out
    second = run(tool.execute(msg, name="demo", action="read"))
    assert "Already returned" in second


def test_unknown_action_lists_the_real_ones(tool):
    out = run(tool.execute(_msg(), name="demo", action="yolo"))
    assert "unknown action" in out
    assert "logs" in out and "write" in out


def test_errors_come_back_as_text_not_exceptions(tool):
    out = run(tool.execute(_msg(), name="demo", action="write", files={"../esc.py": "x"}))
    assert out.startswith("Error:")
    assert "unsafe" in out


# ── extra pip packages ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "--index-url=http://evil.test/simple",
        "git+https://evil.test/pkg.git",
        "pkg; rm -rf /",
        "pkg --upgrade",
        "-r requirements.txt",
        "http://evil.test/pkg.whl",
    ],
)
def test_package_names_cannot_smuggle_flags_or_urls(bad):
    """The list becomes a pip command line, so nothing but names gets through."""
    with pytest.raises(site_server.SiteServerError):
        site_server.parse_packages([bad])


def test_package_names_accept_pins_and_extras():
    assert site_server.parse_packages(["redis==5.0.1", "numpy"]) == ["redis==5.0.1", "numpy"]
    assert site_server.parse_packages("redis, numpy") == ["redis", "numpy"]
    assert site_server.parse_packages('["uvicorn[standard]"]') == ["uvicorn[standard]"]
    assert site_server.parse_packages(None) == []


def test_too_many_packages_is_refused():
    with pytest.raises(site_server.SiteServerError, match="too many"):
        site_server.parse_packages([f"pkg{i}" for i in range(site_server.MAX_PACKAGES + 1)])


def test_no_packages_means_the_shared_image(data_dir):
    assert run(site_server.build_site_image(data_dir, "demo", [])) == site_server.IMAGE


def test_packages_build_a_per_site_image(data_dir, monkeypatch):
    seen = {}

    async def fake_docker(*args, **kw):
        if args[0] == "build":
            seen["tag"] = args[2]
            seen["dockerfile"] = (Path(args[3]) / "Dockerfile").read_text()
        return 0, "", ""

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    tag = run(site_server.build_site_image(data_dir, "demo", ["redis==5.0.1"]))
    # The image tag must NOT collide with the container name, or `docker
    # inspect` finds the image after the container is gone.
    assert tag == "maxwell-siteimg-demo" == seen["tag"]
    assert tag != site_server.container_name("demo")
    assert "FROM maxwell-site-runtime" in seen["dockerfile"]
    assert "pip install --no-cache-dir redis==5.0.1" in seen["dockerfile"]
    # The build must not run as root at the end.
    assert seen["dockerfile"].rstrip().endswith("USER site")


def test_a_failed_package_build_explains_itself(data_dir, monkeypatch):
    async def fake_docker(*args, **kw):
        if args[0] == "build":
            return 1, "", "ERROR: No matching distribution found for nosuchpkg"
        return 0, "", ""

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    with pytest.raises(site_server.SiteServerError, match="No matching distribution"):
        run(site_server.build_site_image(data_dir, "demo", ["nosuchpkg"]))


def test_rewriting_code_keeps_the_build_dir(data_dir):
    """_build holds the per-site Dockerfile; a redeploy must not delete it."""
    site_server.write_code(data_dir, "demo", {"app.py": "v1"})
    build = site_server.code_dir(data_dir, "demo") / "_build"
    build.mkdir(exist_ok=True)
    (build / "Dockerfile").write_text("FROM x")
    site_server.write_code(data_dir, "demo", {"app.py": "v2"})
    assert (build / "Dockerfile").read_text() == "FROM x"


def test_image_tag_never_collides_with_the_container_name():
    """They shared a prefix once; `inspect` then answered for the image after
    the container was removed, and every deploy stalled on the removal wait."""
    for slug in ("demo", "my-site", "ab"):
        assert site_server.container_name(slug) != site_server.IMAGE_PREFIX + slug


def test_container_lookups_are_scoped_to_containers(data_dir, monkeypatch):
    """Every inspect must pass --type container for the same reason."""
    calls = []

    async def fake_docker(*args, **kw):
        calls.append(args)
        return (1, "", "no such object") if args[0] == "inspect" else (0, "", "")

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    run(site_server._remove_container("demo"))
    run(site_server.status(data_dir, "demo"))
    inspects = [a for a in calls if a[0] == "inspect"]
    assert inspects, "expected inspect calls"
    for args in inspects:
        assert "--type" in args and "container" in args, args


def test_wait_healthy_accepts_a_restarted_container_that_is_serving(monkeypatch):
    """RestartCount is sticky; a serving container is healthy even after one crash."""

    async def fake_docker(*args, **kw):
        return 0, "true false 1 0", ""

    async def fake_ping(_port):
        return "ok"

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    monkeypatch.setattr(site_server, "_http_ping", fake_ping)
    assert run(site_server._wait_healthy(8800, "demo")) == "ok"


def test_wait_healthy_reports_a_real_crash_loop(monkeypatch):
    monkeypatch.setattr(site_server, "CRASH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(site_server, "START_TIMEOUT", 2.0)

    async def fake_docker(*args, **kw):
        return 0, "false true 3 0", ""

    async def fake_ping(_port):
        return "ConnectionRefusedError"

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    monkeypatch.setattr(site_server, "_http_ping", fake_ping)
    out = run(site_server._wait_healthy(8800, "demo"))
    assert "crashing" in out
    assert "exit code 0" in out
