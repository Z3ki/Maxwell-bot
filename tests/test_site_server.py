"""Site backend servers: input validation, isolation, lifecycle bookkeeping.

The parts that talk to docker are exercised against a fake `docker` so the
suite stays hermetic; what is checked here is everything that decides WHAT
docker gets asked to do — because that is where a path escape, a leaked
secret, or a cross-site reach would come from.
"""

import asyncio
import json
import os
import sqlite3
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


def test_legacy_database_survives_permission_repair_when_chown_is_denied(data_dir, monkeypatch):
    path = site_server.state_dir(data_dir, "demo")
    path.mkdir(parents=True)
    database = path / "app.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE notes (text TEXT)")
        conn.execute("INSERT INTO notes VALUES ('keep me')")
    database.chmod(0o600)

    def denied(*_args, **_kwargs):
        raise PermissionError("no chown capability")

    monkeypatch.setattr(os, "fchown", denied)
    site_server.prepare_state_dir(data_dir, "demo")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT text FROM notes").fetchall() == [("keep me",)]
    # uid 10001 can update the old root-owned file as well as create its journal.
    assert database.stat().st_mode & 0o006 == 0o006
    assert path.stat().st_mode & 0o003 == 0o003


def test_rewriting_replaces_source_but_keeps_the_database(data_dir, monkeypatch):
    # This test covers source replacement, not privileged uid remapping.
    # Use the mapped runtime uid in rootless CI; permission repair has its
    # own tests immediately above and in the symlink-safety cases below.
    monkeypatch.setattr(site_server, "SITE_UID", os.getuid())
    monkeypatch.setattr(site_server, "SITE_GID", os.getgid())
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
        if args[0] == "inspect":
            return 1, "", "No such container"
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


@pytest.mark.parametrize("action", ["write", "read", "env", "logs", "restart", "stop"])
def test_another_user_cannot_access_the_backend(tool, action):
    out = run(tool.execute(
        _msg(uid=999), name="demo", action=action,
        files={"app.py": "print('ok')"},
    ))
    assert out.startswith("Error:")
    assert not tool._started
    assert not site_server.code_dir(tool.bot.config.DATA_DIR, "demo").exists()


def test_admin_can_edit_another_users_backend(tool):
    tool.bot._is_admin = lambda uid: uid == 999
    out = run(tool.execute(
        _msg(uid=999), name="demo", action="write", files={"app.py": "print('ok')"},
    ))
    assert "Backend server live" in out
    assert tool._started


@pytest.mark.parametrize("still_running", [True, False])
def test_tool_failure_preserves_actual_backend_running_state(tool, monkeypatch, still_running):
    site_server._write_entry(
        tool.bot.config.DATA_DIR, "demo",
        {"port": 8888, "running": still_running},
    )
    tool.bot._sites["demo"]["server"] = True

    async def reject(*args, **kwargs):
        raise site_server.SiteServerError("cannot deploy")

    monkeypatch.setattr(site_server, "start", reject)
    out = run(tool.execute(_msg(), name="demo", action="restart"))
    assert out.startswith("Error:")
    assert tool.bot._sites["demo"]["server"] is still_running


def test_server_read_returns_a_huge_file_and_refuses_a_repeat(tool):
    huge = "print(1)\n" + ("#x\n" * 12_000)
    msg = _msg()
    run(tool.execute(msg, name="demo", action="write", files={"app.py": huge}))
    out = run(tool.execute(msg, name="demo", action="read"))
    assert huge in out
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


def test_legacy_packages_reuse_existing_image_without_building(data_dir, monkeypatch):
    site_server._write_entry(data_dir, "demo", {"packages": ["redis==5.0.1"]})

    async def fake_docker(*args, **kwargs):
        assert args[:2] == ("image", "inspect"), "no builds may be requested"
        return 0, "", ""

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    assert run(site_server.build_site_image(data_dir, "demo", ["redis==5.0.1"])) == "maxwell-siteimg-demo"


def test_new_dependency_requests_are_refused(data_dir):
    with pytest.raises(site_server.SiteServerError, match="new extra packages"):
        run(site_server.build_site_image(data_dir, "demo", ["redis==5.0.1"]))


def test_missing_legacy_image_has_actionable_error(data_dir, monkeypatch):
    site_server._write_entry(data_dir, "demo", {"packages": ["redis==5.0.1"]})

    async def fake_docker(*args, **kwargs):
        return 1, "", "No such image"

    monkeypatch.setattr(site_server, "_docker", fake_docker)
    with pytest.raises(site_server.SiteServerError, match="restore that existing image"):
        run(site_server.build_site_image(data_dir, "demo", ["redis==5.0.1"]))


def test_rewriting_code_keeps_the_build_dir(data_dir):
    """_build holds the per-site Dockerfile; a redeploy must not delete it."""
    site_server.write_code(data_dir, "demo", {"app.py": "v1"})
    build = site_server.code_dir(data_dir, "demo") / "_build"
    build.mkdir(exist_ok=True)
    (build / "Dockerfile").write_text("FROM x")
    site_server.write_code(data_dir, "demo", {"app.py": "v2"})
    assert (build / "Dockerfile").read_text() == "FROM x"


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


@pytest.fixture
def sandbox_host(data_dir, monkeypatch):
    policy = data_dir / "policy"
    cgroup = data_dir / "cgroup"
    policy.mkdir()
    cgroup.mkdir()
    (policy / "resource-pool-ready").write_text(site_server.POOL_READY_TOKEN)
    for name, value in {
        "memory.max": str(site_server.POOL_MEMORY),
        "memory.swap.max": "0",
        "cpu.max": "200000 100000",
        "pids.max": str(site_server.POOL_TASKS),
    }.items():
        (cgroup / name).write_text(value)
    monkeypatch.setattr(site_server, "POOL_READY_DIR", policy)
    monkeypatch.setattr(site_server, "POOL_CGROUP", cgroup)
    monkeypatch.setattr(site_server, "_LIFECYCLE_LOCK", asyncio.Lock())
    containers = {}
    state = {"removed": [], "info": {
        "OSType": "linux", "CgroupDriver": "systemd", "CgroupVersion": "2",
        "Runtimes": {"runsc": {}},
    }}

    async def docker(*args, **kwargs):
        if args[0] == "info":
            return 0, json.dumps(state["info"]), ""
        if args[0] == "ps":
            if state.get("capacity_error"):
                return 1, "", "daemon unavailable"
            return 0, "\n".join(json.dumps({"Names": name, "Labels": label})
                                for name, label in containers.items()), ""
        if args[0] == "image":
            if state.get("image_missing"):
                return 1, "", "No such image"
            return 0, "", ""
        if args[0] == "rm":
            state["removed"].append(args[-1])
            containers.pop(args[-1], None)
            return 0, "", ""
        if args[0] == "inspect":
            if args[-1] in containers:
                return 0, "id", ""
            return 1, "", "No such container"
        if args[0] == "run":
            name = args[args.index("--name") + 1]
            containers[name] = "maxwell.site=" + name.removeprefix(site_server.CONTAINER_PREFIX)
            return 0, "id", ""
        raise AssertionError(args)

    async def healthy(*args):
        await asyncio.sleep(0)  # exercise overlapping lifecycle calls
        return "ok"

    async def ping(*args):
        return "not listening"

    monkeypatch.setattr(site_server, "_docker", docker)
    monkeypatch.setattr(site_server, "_wait_healthy", healthy)
    monkeypatch.setattr(site_server, "_http_ping", ping)
    monkeypatch.setattr(site_server, "_port_is_free", lambda port: True)
    state.update(containers=containers, policy=policy, cgroup=cgroup)
    return state


def _live_backend(data_dir, sandbox_host):
    site_server.write_code(data_dir, "demo", {"app.py": "original code"})
    entry = {"running": True, "port": 8800, "env": {"API_KEY": "retained"}}
    site_server._write_entry(data_dir, "demo", entry)
    sandbox_host["containers"][site_server.container_name("demo")] = "maxwell.site=demo"
    return entry


@pytest.mark.parametrize("failure", ["marker", "cgroup", "memory", "swap", "cpu", "tasks", "runtime", "driver"])
def test_unready_host_never_replaces_live_backend(data_dir, sandbox_host, failure):
    previous = _live_backend(data_dir, sandbox_host)
    if failure == "marker":
        (sandbox_host["policy"] / "resource-pool-ready").unlink()
    elif failure == "cgroup":
        (sandbox_host["cgroup"] / "cpu.max").unlink()
    elif failure in {"memory", "swap", "cpu", "tasks"}:
        name, value = {
            "memory": ("memory.max", "max"),
            "swap": ("memory.swap.max", "1"),
            "cpu": ("cpu.max", "300000 100000"),
            "tasks": ("pids.max", "max"),
        }[failure]
        (sandbox_host["cgroup"] / name).write_text(value)
    elif failure == "runtime":
        sandbox_host["info"]["Runtimes"] = {}
    else:
        sandbox_host["info"]["CgroupDriver"] = "cgroupfs"
    with pytest.raises(site_server.SiteServerError, match="setup_site_host.sh"):
        run(site_server.start(data_dir, "demo"))
    assert sandbox_host["removed"] == []
    assert site_server.get_entry(data_dir, "demo") == previous
    assert site_server.read_code(data_dir, "demo", "app.py") == "original code"


def test_capacity_counts_stopped_orphaned_and_label_only_containers(data_dir, sandbox_host):
    previous = _live_backend(data_dir, sandbox_host)
    containers = sandbox_host["containers"]
    # No registry entries for these: labels and Docker names, not registry state,
    # define the real number of backend slots.
    for index in range(site_server.MAX_BACKENDS):
        name = f"orphan-{index}" if index % 2 else f"maxwell-site-orphan-{index}"
        containers[name] = f"maxwell.site=orphan-{index}"
    with pytest.raises(site_server.SiteServerError, match="capacity reached"):
        run(site_server.start(data_dir, "demo"))
    assert sandbox_host["removed"] == []
    assert site_server.get_entry(data_dir, "demo") == previous


def test_replacement_keeps_its_slot_at_exact_capacity(data_dir, sandbox_host):
    _live_backend(data_dir, sandbox_host)
    for index in range(site_server.MAX_BACKENDS - 1):
        sandbox_host["containers"][f"maxwell-site-other-{index}"] = ""
    result = run(site_server.start(data_dir, "demo"))
    assert result["running"] is True
    assert result["env"] == {"API_KEY": "retained"}
    assert len(sandbox_host["containers"]) == site_server.MAX_BACKENDS


def test_overlapping_starts_cannot_both_take_the_last_slot(data_dir, sandbox_host):
    for index in range(site_server.MAX_BACKENDS - 1):
        sandbox_host["containers"][f"maxwell-site-other-{index}"] = ""
    for slug in ("first", "second"):
        site_server.write_code(data_dir, slug, {"app.py": "server"})

    async def scenario():
        return await asyncio.gather(
            site_server.start(data_dir, "first"), site_server.start(data_dir, "second"),
            return_exceptions=True,
        )

    results = run(scenario())
    assert results[0]["running"] is True
    assert isinstance(results[1], site_server.SiteServerError)
    assert "capacity reached" in str(results[1])
    assert len(sandbox_host["containers"]) == site_server.MAX_BACKENDS


def test_failed_capacity_query_cannot_remove_existing_app(data_dir, sandbox_host):
    previous = _live_backend(data_dir, sandbox_host)
    sandbox_host["capacity_error"] = True
    with pytest.raises(site_server.SiteServerError, match="cannot check backend capacity"):
        run(site_server.start(data_dir, "demo"))
    assert sandbox_host["removed"] == []
    assert site_server.get_entry(data_dir, "demo") == previous


def test_permission_failure_rejects_before_replacement(data_dir, sandbox_host, monkeypatch):
    previous = _live_backend(data_dir, sandbox_host)

    def denied(*args, **kwargs):
        raise PermissionError("persistent directory inaccessible")

    monkeypatch.setattr(os, "fchmod", denied)
    with pytest.raises(site_server.SiteServerError, match="cannot prepare persistent backend data"):
        run(site_server.start(data_dir, "demo"))
    assert sandbox_host["removed"] == []
    assert site_server.get_entry(data_dir, "demo") == previous


def test_state_symlinks_are_rejected_without_changing_the_target(data_dir):
    state = site_server.state_dir(data_dir, "demo")
    state.mkdir(parents=True)
    outside = data_dir / "outside"
    outside.write_text("private")
    before = outside.stat().st_mode
    (state / "app.db").symlink_to(outside)
    with pytest.raises(site_server.SiteServerError, match="unsupported file"):
        site_server.prepare_state_dir(data_dir, "demo")
    assert outside.read_text() == "private"
    assert outside.stat().st_mode == before


def test_failed_teardown_never_deletes_persistent_state(data_dir, monkeypatch):
    site_server.write_code(data_dir, "demo", {"app.py": "keep"})
    database = site_server.state_dir(data_dir, "demo") / "app.db"
    database.write_bytes(b"persistent database")
    site_server._write_entry(data_dir, "demo", {"running": True})

    async def cannot_remove(slug):
        raise site_server.SiteServerError("container still present")

    monkeypatch.setattr(site_server, "_remove_container", cannot_remove)
    with pytest.raises(site_server.SiteServerError, match="container still present"):
        run(site_server.destroy(data_dir, "demo"))
    assert database.read_bytes() == b"persistent database"
    assert site_server.get_entry(data_dir, "demo")["running"] is True


def test_reconciliation_keeps_missing_backends_restart_config(data_dir, monkeypatch):
    previous = {"running": True, "env": {"API_KEY": "retained"}, "packages": ["redis==5.0.1"]}
    site_server._write_entry(data_dir, "demo", previous)

    async def absent(*args, **kwargs):
        return 1, "", "No such container"

    monkeypatch.setattr(site_server, "_docker", absent)
    run(site_server.reconcile(data_dir))
    entry = site_server.get_entry(data_dir, "demo")
    assert entry["running"] is False
    assert entry["env"] == previous["env"]
    assert entry["packages"] == previous["packages"]


def test_missing_shared_runtime_never_replaces_live_app(data_dir, sandbox_host):
    previous = _live_backend(data_dir, sandbox_host)
    sandbox_host["image_missing"] = True
    with pytest.raises(site_server.SiteServerError, match="docker build"):
        run(site_server.start(data_dir, "demo"))
    assert sandbox_host["removed"] == []
    assert site_server.get_entry(data_dir, "demo") == previous


def test_changed_dependencies_never_replace_live_app(data_dir, sandbox_host):
    previous = _live_backend(data_dir, sandbox_host)
    with pytest.raises(site_server.SiteServerError, match="new extra packages"):
        run(site_server.start(data_dir, "demo", packages=["redis==5.0.1"]))
    assert sandbox_host["removed"] == []
    assert site_server.get_entry(data_dir, "demo") == previous


def test_lifecycle_waits_for_other_process_lock(data_dir, sandbox_host):
    _live_backend(data_dir, sandbox_host)

    async def scenario():
        with site_server.FileLock(data_dir / "site_servers_lifecycle", timeout=0):
            pending = asyncio.create_task(site_server.start(data_dir, "demo"))
            await asyncio.sleep(0.1)
            assert not pending.done()
            assert sandbox_host["removed"] == []
        return await pending

    assert run(scenario())["running"] is True


def test_state_file_link_swap_cannot_change_external_permissions(data_dir, monkeypatch):
    state = site_server.state_dir(data_dir, "demo")
    state.mkdir(parents=True)
    database = state / "app.db"
    database.write_text("old database")
    outside = data_dir / "outside"
    outside.write_text("private")
    outside.chmod(0o600)
    original_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if path == "app.db":
            database.unlink()
            database.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(site_server.SiteServerError, match="cannot prepare persistent backend data"):
        site_server.prepare_state_dir(data_dir, "demo")
    assert outside.read_text() == "private"
    assert outside.stat().st_mode & 0o777 == 0o600
