"""Request-scoped workers retain caller authorization and stop without orphan jobs."""

import asyncio
import json
from datetime import datetime, timezone
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot, ToolCircuitBreaker, _current_request_provider
from maxwell_core.providers.errors import ProviderEmptyResponseError
from maxwell_core.providers.models import ProviderResult
from message_quota import MessageQuota
from plugins.maxwell_extras.task_workers import DelegateTaskTool, _inside_worker
from plugins.maxwell_extras.task_jobs import format_job
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


def call(name="shell", /, **args):
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


def test_worker_recovers_empty_response_after_tool_without_replaying_it(tmp_path, monkeypatch):
    bot, msg, shell, worker = setup(tmp_path)
    monkeypatch.setattr("plugins.maxwell_extras.task_workers.asyncio.sleep", AsyncMock())
    bot._generate_response.side_effect = [
        call(),
        ProviderEmptyResponseError("Empty response from provider"),
        ProviderResult("Verified task result"),
    ]
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    assert result["report"] == "Verified task result"
    assert result["completed"] == [{"tool": "shell", "status": "returned"}]
    shell.execute.assert_awaited_once()
    assert bot._generate_response.await_count == 3


def test_worker_reports_exhausted_empty_responses_without_replaying_tool(tmp_path, monkeypatch):
    bot, msg, shell, worker = setup(tmp_path)
    monkeypatch.setattr("plugins.maxwell_extras.task_workers.asyncio.sleep", AsyncMock())
    bot._generate_response.side_effect = [
        call(),
        *[ProviderEmptyResponseError("Empty response from provider") for _ in range(3)],
    ]
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "failed"
    assert "ProviderEmptyResponseError" in result["report"]
    assert result["completed"] == [{"tool": "shell", "status": "returned"}]
    shell.execute.assert_awaited_once()
    assert bot._generate_response.await_count == 4


def test_worker_does_not_retry_after_request_revoked(tmp_path, monkeypatch):
    bot, msg, shell, worker = setup(tmp_path)
    monkeypatch.setattr("plugins.maxwell_extras.task_workers.asyncio.sleep", AsyncMock())

    async def empty_then_revoke(*args, **kwargs):
        bot._control["subagents_require_premium"] = True
        bot._message_quota.configure("123", plan="free")
        raise ProviderEmptyResponseError("Empty response from provider")

    bot._generate_response.side_effect = empty_then_revoke
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "failed"
    assert bot._generate_response.await_count == 1
    shell.execute.assert_not_awaited()


def test_free_plan_runs_while_the_premium_gate_is_open(tmp_path):
    bot, msg, shell, worker = setup(tmp_path, premium=False)
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    shell.execute.assert_awaited_once()


def test_premium_gate_fails_closed_when_restored(tmp_path):
    bot, msg, shell, worker = setup(tmp_path, premium=False)
    bot._control["subagents_require_premium"] = True
    assert "requires" in asyncio.run(worker.run(msg, tasks=TASKS))
    bot._message_quota.plan_for = lambda uid: (_ for _ in ()).throw(
        OSError("private-error")
    )
    result = asyncio.run(worker.run(msg, tasks=TASKS))
    assert "could not be verified" in result and "private-error" not in result
    shell.execute.assert_not_awaited()


def test_plan_is_durable_and_revocation_stops_before_next_tool(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._control["subagents_require_premium"] = True
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
    bot._generate_response.side_effect = [call(name, **args), ProviderResult("continued")]
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    assert result["report"] == "continued"
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
    # Long enough for both workers to reach the blocking model call before the
    # deadline; 10 ms could expire during provider selection on a busy runner.
    monkeypatch.setattr(
        "plugins.maxwell_extras.task_workers.worker_job_timeout_seconds", lambda *a, **k: 0.5
    )
    result = asyncio.run(worker.run(msg, tasks=TASKS * 2))
    assert "deadline" in result and len(stopped) == 2
    assert worker._active == 0 and not worker._users
    monkeypatch.setattr(
        "plugins.maxwell_extras.task_workers.worker_job_timeout_seconds", lambda *a, **k: 30
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


def test_text_only_reply_is_not_a_finished_website(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._generate_response.side_effect = [
        ProviderResult("I'll build the complete game and publish it."),
        call(),
        ProviderResult("Published the site."),
    ]
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    assert result["report"] == "Published the site."
    shell.execute.assert_awaited_once()
    second_prompt = bot._generate_response.await_args_list[1].args[0]
    assert any(
        "have not called any tools" in str(row.get("content", ""))
        for row in second_prompt
    )


@pytest.mark.parametrize(
    "base_url",
    ["https://maxwell.z3ki.dev", "https://maxwell.z3ki.dev/dev"],
)
def test_published_worker_completion_links_real_site_tool_result(tmp_path, base_url):
    from bot_tools import CreateSiteTool

    bot, msg, _, worker = setup(tmp_path)
    bot.config.MAXWELL_SITE_DIR = str(tmp_path / "public" / "bot")
    bot.config.MAXWELL_PUBLIC_BASE_URL = base_url
    bot.config.DATA_DIR = str(tmp_path)
    bot._sites = {}
    msg.author.display_name = "Requester"
    bot.tools["create_site"] = CreateSiteTool(bot)
    page = "<!DOCTYPE html><html><head><title>Garden</title></head><body><h1>Garden</h1></body></html>"
    bot._generate_response.side_effect = [
        call("create_site", name="real-garden", title="A different title", body=page),
        ProviderResult("Published an amazing garden with a rich visual design and custom controls."),
    ]

    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]

    url = f"{base_url}/bot/real-garden/"
    assert result["status"] == "reported"
    assert result["completed"] == [
        {"tool": "create_site", "status": "returned", "site_url": url}
    ]
    assert (tmp_path / "public" / "bot" / "real-garden" / "index.html").is_file()
    assert format_job({"state": "completed", "reports": [result]}) == f"here you go: {url}"


@pytest.mark.parametrize(
    "name, output, expected_url",
    [
        ("create_site", "Site created: https://maxwell.z3ki.dev/bot/demo/\nLifetime: 24h.", "https://maxwell.z3ki.dev/bot/demo/"),
        ("edit_site", "Wrote index.html, style.css → https://maxwell.z3ki.dev/bot/demo/", "https://maxwell.z3ki.dev/bot/demo/"),
        ("edit_site", "Patched index.html (2 occurrence(s)) → https://maxwell.z3ki.dev/bot/demo/", "https://maxwell.z3ki.dev/bot/demo/"),
        ("create_site", "Error: could not publish https://maxwell.z3ki.dev/bot/demo/", None),
        ("create_site", "Site created: missing URL", None),
        ("edit_site", "https://maxwell.z3ki.dev/bot/demo/\n  • index.html (100 bytes)", None),
        ("edit_site", "<p>Site created: https://maxwell.z3ki.dev/bot/invented/</p>", None),
        ("shell", "Site created: https://maxwell.z3ki.dev/bot/demo/", None),
    ],
)
def test_worker_only_keeps_links_from_successful_site_writes(
    tmp_path, name, output, expected_url
):
    bot, msg, tool, worker = setup(tmp_path)
    tool.tool_name = name
    tool.execute.return_value = output
    bot.tools = {name: tool}
    bot._generate_response.side_effect = [
        call(name, command="pwd"),
        ProviderResult("The worker's final report."),
    ]

    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]

    completion = result["completed"][0]
    assert completion.get("site_url") == expected_url
    assert completion["status"] == ("error" if output.startswith("Error") else "returned")
    text = format_job({"state": "completed", "reports": [result]})
    assert text == (
        f"here you go: {expected_url}" if expected_url else "The worker's final report."
    )


def test_publication_completion_keeps_multiple_sites_and_other_results():
    urls = [
        "https://maxwell.z3ki.dev/bot/first/",
        "https://maxwell.z3ki.dev/bot/second/",
    ]
    publication = {
        "status": "reported",
        "completed": [
            {"tool": "create_site", "status": "returned", "site_url": urls[0]},
            {"tool": "edit_site", "status": "returned", "site_url": urls[0]},
            {"tool": "create_site", "status": "returned", "site_url": urls[1]},
        ],
        "report": "Published two impressive sites without mentioning their links.",
    }
    other = {"status": "reported", "report": "Saved the notes in /workspace/notes.txt."}
    row = {"state": "completed", "reports": [publication, other]}
    assert format_job(row) == (
        f"here you go: {urls[0]}\nhere you go: {urls[1]}\n"
        "Saved the notes in /workspace/notes.txt."
    )


def test_failed_publication_job_preserves_failure_output():
    row = {
        "id": "abc123",
        "state": "incomplete",
        "effects": True,
        "reports": [{
            "status": "failed",
            "completed": [{
                "tool": "create_site",
                "status": "returned",
                "site_url": "https://maxwell.z3ki.dev/bot/partial/",
            }],
            "report": "Site edits failed.",
        }],
    }
    text = format_job(row)
    assert "didn't finish" in text
    assert "failed — Site edits failed." in text
    assert "may have already changed files" in text
    assert "here you go:" not in text


def test_text_only_reply_stops_after_two_nudges(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._generate_response.side_effect = lambda *a, **k: ProviderResult(
        "I'll build the complete game and publish it."
    )
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "no_tools"
    assert shell.execute.await_count == 0
    assert bot._generate_response.await_count == 3


def test_worker_keeps_tool_output_up_to_the_context_window(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot.ai_provider.capabilities.context_window = 256000
    bot.ai_provider.max_tokens = 32768
    long_output = "x" * 20000
    shell.execute = AsyncMock(return_value=long_output)
    seen = []

    async def generate(messages, **kwargs):
        seen.append(messages)
        if len(seen) == 1:
            return call()
        return ProviderResult("Published the site.")

    bot._generate_response.side_effect = generate
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    tool_rows = [row for row in seen[1] if row.get("role") == "tool"]
    tool_text = tool_rows[-1]["content"]
    assert tool_text.endswith(long_output)
    assert len(tool_text) > 8000
    assert bot._generate_response.await_args.kwargs["max_tokens"] == 32768


def test_large_tool_output_is_shared_and_still_read(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    shell.execute = AsyncMock(return_value='"' * 200000)
    batch = call()
    batch.tool_calls.append({**batch.tool_calls[0], "id": "call_2"})
    seen = []

    async def generate(messages, **kwargs):
        seen.append(messages)
        return batch if len(seen) == 1 else ProviderResult("Done.")

    bot._generate_response.side_effect = generate
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    tool_rows = [row for row in seen[1] if row.get("role") == "tool"]
    first, second = tool_rows[-2:]
    assert first["content"].endswith("[output truncated]")
    assert first["content"].count('"') > 1000
    assert second["content"].count('"') > 1000


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


def test_worker_injects_round_and_remaining_budget(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    seen = []

    async def generate(messages, **kwargs):
        seen.append(messages)
        return call() if len(seen) == 1 else ProviderResult("Report ready")

    bot._generate_response.side_effect = generate
    result = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "reported"
    notes = [
        row["content"]
        for row in seen[0]
        if row.get("role") == "system" and "[Worker budget]" in row.get("content", "")
    ]
    assert notes and "Round 1 of" in notes[0]
