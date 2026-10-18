"""Request-scoped workers retain caller authorization and stop without orphan jobs."""

import asyncio
import json
from datetime import datetime, timezone
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot, ToolCircuitBreaker, _current_request_provider
from maxwell_core.providers.models import ProviderResult
from message_quota import MessageQuota
from plugins.maxwell_extras.task_workers import DelegateTaskTool, _inside_worker
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from tools import Tool


class FakeTool(Tool):
    tool_name = "shell"
    returns_result = True
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}, "files": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    }

    def get_description(self):
        return "Execute a scoped shell command"

    async def execute(self, message, **kwargs):
        return "verified"


def call(name="shell", **args):
    return ProviderResult(
        "",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args or {"command": "pwd"}),
                },
            }
        ],
    )


def setup(tmp_path, *, premium=True):
    ledger = MessageQuota(tmp_path / "quota.sqlite3")
    ledger.configure("123", plan="premium" if premium else "free")
    bot = SimpleNamespace(
        _message_quota=ledger,
        _control={},
        plugin_manager=None,
        config=SimpleNamespace(MAXWELL_OWNER_IDS={"999"}),
        _message_tool_platform=lambda msg: "discord",
        _is_admin=lambda uid: False,
        _tool_breaker=ToolCircuitBreaker(),
        hooks=None,
        is_message_tainted=lambda msg: False,
        _record_llm_trace=AsyncMock(),
        ai_provider=SimpleNamespace(
            capabilities=SimpleNamespace(context_window=32000), max_tokens=4096
        ),
        _generate_response=AsyncMock(
            side_effect=[call(), ProviderResult("Verified task result")]
        ),
    )
    shell = FakeTool(bot)
    shell.execute = AsyncMock(return_value="verified")
    bot.tools = {"shell": shell}
    bot._compatible_tool_names = lambda platform: set(bot.tools)
    bot._execute_tool_by_name = MethodType(MaxwellBot._execute_tool_by_name, bot)
    message = SimpleNamespace(
        id=1,
        author=SimpleNamespace(id=123, bot=False),
        channel=SimpleNamespace(id=45),
        guild=None,
        created_at=datetime.now(timezone.utc),
    )
    return bot, message, shell, DelegateTaskTool(bot)


TASKS = [{"task": "Inspect workspace and report"}]


def test_premium_worker_executes_via_real_authorization_and_does_not_save_messages(
    tmp_path,
):
    bot, msg, shell, worker = setup(tmp_path)
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    assert result["completed"] == [{"tool": "shell", "status": "returned"}]
    shell.execute.assert_awaited_once_with(msg, command="pwd")
    assert bot._generate_response.await_count == 2
    assert all(
        c.kwargs["charge_message"] is False
        for c in bot._generate_response.await_args_list
    )
    assert worker._active == 0 and not worker._users and not _inside_worker.get()
    assert {p.name for p in tmp_path.iterdir()} == {"quota.sqlite3"}
    assert "delegate_task" not in {
        s["function"]["name"]
        for s in bot._generate_response.await_args_list[0].kwargs["tools"]
    }


def test_free_plan_and_unverified_entitlement_fail_closed(tmp_path):
    bot, msg, shell, worker = setup(tmp_path, premium=False)
    assert "requires" in asyncio.run(worker.run(msg, tasks=TASKS))
    bot._message_quota.plan_for = lambda uid: (_ for _ in ()).throw(
        OSError("private-error")
    )
    result = asyncio.run(worker.run(msg, tasks=TASKS))
    assert "could not be verified" in result and "private-error" not in result
    shell.execute.assert_not_awaited()


def test_plan_is_durable_and_revocation_stops_before_next_tool(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._message_quota = MessageQuota(tmp_path / "quota.sqlite3")

    async def generate(*a, **k):
        bot._message_quota.configure("123", plan="free")
        return call()

    bot._generate_response.side_effect = generate
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "failed"
    shell.execute.assert_not_awaited()


def test_deleted_request_cannot_continue_worker(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._user_preferences = UserPreferenceStore(tmp_path / "prefs.json")

    async def generate(*a, **k):
        bot._user_preferences.delete_personal("123")
        return call()

    bot._generate_response.side_effect = generate
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "failed"
    shell.execute.assert_not_awaited()


def test_worker_cannot_bypass_new_tool_disable(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    responses = iter([call(), ProviderResult("Tool was refused")])

    async def generate(*a, **k):
        bot._control["disabled_tools"] = ["shell"]
        return next(responses)

    bot._generate_response.side_effect = generate
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["completed"] == [{"tool": "shell", "status": "error"}]
    shell.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "name,args",
    [
        ("send_message", {"content": "hi"}),
        ("delegate_task", {"tasks": TASKS}),
        ("shell", {"command": "ls", "files": "secret.txt"}),
        ("shell", {"command": "ls", "_owner": "999"}),
    ],
)
def test_worker_rejects_escalation_recursion_and_exports(tmp_path, name, args):
    bot, msg, shell, worker = setup(tmp_path)
    bot._generate_response.side_effect = [call(name, **args)]
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "failed"
    shell.execute.assert_not_awaited()


def test_exception_report_never_leaks_provider_exception_content(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._generate_response.side_effect = RuntimeError("secret-key-123")
    result = asyncio.run(worker.run(msg, tasks=TASKS))
    assert "failed" in result and "secret-key" not in result


def test_timeout_and_parent_cancel_join_all_children(tmp_path, monkeypatch):
    bot, msg, shell, worker = setup(tmp_path)
    stopped = []
    entered = asyncio.Event()

    async def generate(*a, **k):
        if bot._generate_response.await_count >= 4:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    bot._generate_response.side_effect = generate
    monkeypatch.setattr(
        "plugins.maxwell_extras.task_workers.worker_timeout_seconds", lambda *a: 0.01
    )
    result = asyncio.run(worker.run(msg, tasks=TASKS * 2))
    assert "deadline" in result and len(stopped) == 2
    assert worker._active == 0 and not worker._users
    monkeypatch.setattr(
        "plugins.maxwell_extras.task_workers.worker_timeout_seconds", lambda *a: 30
    )

    async def cancel():
        running = asyncio.create_task(worker.run(msg, tasks=TASKS * 2))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(cancel())
    assert len(stopped) == 4 and worker._active == 0


def test_same_user_capacity_is_atomic_and_sibling_model_calls_overlap(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    arrived = asyncio.Event()
    release = asyncio.Event()

    async def generate(*a, **k):
        if bot._generate_response.await_count >= 2:
            arrived.set()
        await release.wait()
        return ProviderResult("Finished")

    bot._generate_response.side_effect = generate

    async def run():
        first = asyncio.create_task(worker.run(msg, tasks=TASKS * 2))
        await arrived.wait()
        assert "busy" in await worker.run(msg, tasks=TASKS)
        assert worker._active == 2
        release.set()
        assert len(json.loads(await first)) == 2

    asyncio.run(run())


def test_step_and_context_limits_stop_without_forgetting_writes(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._control["task_workers_max_steps"] = 1
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "step_limit" and shell.execute.await_count == 1
    bot.ai_provider.capabilities.context_window = 1024
    bot._generate_response.reset_mock()
    result = json.loads(
        asyncio.run(worker.run(msg, tasks=[{"task": "x" * 6000}]))
    )[0]
    assert result["status"] == "context_limit"
    bot._generate_response.assert_not_awaited()


def test_worker_uses_request_local_provider_and_limits(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    provider = SimpleNamespace(
        policy=SimpleNamespace(max_request_seconds=1200),
        turn_timeout_seconds=2400,
        capabilities=SimpleNamespace(context_window=12000),
        max_tokens=2000,
    )
    bot._generate_response.side_effect = [ProviderResult("done")]
    token = _current_request_provider.set(provider)
    try:
        asyncio.run(worker.run(msg, tasks=TASKS))
    finally:
        _current_request_provider.reset(token)
    args = bot._generate_response.await_args.kwargs
    assert (
        args["provider"] is provider
        and args["timeout"] == 1200
        and args["max_tokens"] == 2000
    )


@pytest.mark.parametrize(
    "tasks", [None, [], TASKS * 3, [{"kind": "admin", "task": "change config"}]]
)
def test_invalid_tasks_do_not_start_anything(tmp_path, tasks):
    bot, msg, shell, worker = setup(tmp_path)
    assert "Error" in asyncio.run(worker.run(msg, tasks=tasks))
    bot._generate_response.assert_not_awaited()
