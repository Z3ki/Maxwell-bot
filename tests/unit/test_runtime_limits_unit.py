import asyncio
import json
from types import SimpleNamespace

from plugins.maxwell_extras.config_tools import runtime_limits
from plugins.shell.impl import ShellTool
from plugins.shell.isolation import SANDBOX_LIMITS, docker_run_args
from plugins.user_storage.store import UserStorage
from plugins.user_storage.tool import StorageTool
from site_server import boot_failure_excerpt

PREMIUM = "111111111111111111"
FREE = "222222222222222222"


class _Ledger:
    def plan_for(self, uid):
        return "premium" if uid == PREMIUM else "free"


def _bot(tmp_path):
    storage = UserStorage(tmp_path / "st", plan_lookup=_Ledger().plan_for)
    return SimpleNamespace(_message_quota=_Ledger(), _user_storage=storage, tools={})


def test_boot_failure_keeps_the_whole_last_traceback():
    first = (
        'Traceback (most recent call last):\n  File "old.py", line 1\nOldError: gone\n'
    )
    noise = "\n".join(f"booting {n}" for n in range(80))
    last = (
        "Traceback (most recent call last):\n"
        '  File "/app/app.py", line 3, in <module>\n'
        "    import missing_thing\n"
        "ModuleNotFoundError: No module named 'missing_thing'"
    )
    excerpt = boot_failure_excerpt(first + noise + "\n" + last)
    assert excerpt == last
    assert "app.py" in excerpt and "OldError" not in excerpt


def test_boot_failure_without_traceback_keeps_the_end():
    text = "x" * 9000 + "\nport already in use"
    excerpt = boot_failure_excerpt(text, limit=100)
    assert excerpt.startswith("…\n")
    assert excerpt.endswith("port already in use")
    assert boot_failure_excerpt("") == ""


def test_docker_args_use_the_reported_limits():
    args = docker_run_args(
        image="maxwell-shell", container_name="mwsh-" + "a" * 24, runtime="runsc"
    )
    assert args[args.index("--memory") + 1] == SANDBOX_LIMITS["memory"]
    assert args[args.index("--cpus") + 1] == SANDBOX_LIMITS["cpus"]
    assert args[args.index("--pids-limit") + 1] == SANDBOX_LIMITS["pids"]
    assert f"/workspace:rw,exec,nosuid,nodev,size={SANDBOX_LIMITS['workspace']}" in args


def test_shell_limits_follow_the_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_SHELL_TIMEOUT", "120")
    monkeypatch.setenv("MAXWELL_SHELL_PREMIUM_TIMEOUT", "600")
    tool = ShellTool(_bot(tmp_path))
    free = tool.limits_for(FREE)["shell"]
    premium = tool.limits_for(PREMIUM)["shell"]
    assert free["command_timeout_seconds"] == 120
    assert premium["command_timeout_seconds"] == 600
    assert free["persistent_storage_mb"] == 0 and free["survives_idle_reset"] == []
    assert premium["persistent_storage_mb"] == 100 and premium[
        "survives_idle_reset"
    ] == ["/storage"]
    assert premium["memory"] == SANDBOX_LIMITS["memory"]


def test_storage_limits_report_quota_and_address(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://example.test")
    bot = _bot(tmp_path)
    bot._user_storage.set_handle(PREMIUM, "7up")
    bot._user_storage.write(PREMIUM, "public/index.html", b"<h1>hi</h1>")
    tool = StorageTool(bot)
    premium = tool.limits_for(PREMIUM)["storage"]
    assert premium["quota_mb"] == 100
    assert premium["files"] == 1
    assert premium["public_address"] == "https://example.test/u/7up/"
    assert tool.limits_for(FREE)["storage"]["public_address"] == ""


def test_runtime_limits_merges_hooks_and_survives_a_broken_one(tmp_path):
    def broken(_uid):
        raise RuntimeError("nope")

    bot = _bot(tmp_path)
    bot.tools = {
        "a": SimpleNamespace(limits_for=lambda uid: {"a": {"uid": uid}}),
        "b": SimpleNamespace(limits_for=broken),
        "c": SimpleNamespace(),
    }
    merged = asyncio.run(runtime_limits(bot, 42))
    assert merged == {"a": {"uid": "42"}}
    json.dumps(merged)


def test_site_limits_flag_kv_as_public():
    from plugins.sites.impl import SiteServerTool

    limits = SiteServerTool.limits_for(None, "42")
    assert limits["site_kv"]["public_writes"] is True
    assert limits["site_kv"]["safe_for_secrets"] is False
    assert limits["site_backend"]["writable_path"] == "/data"
