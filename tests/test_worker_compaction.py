"""Worker checkpoints keep long tasks moving without replaying completed writes."""

import asyncio
import json

import pytest

from maxwell_core.providers.models import ProviderResult
from test_premium_task_workers import TASKS, call, setup


def test_worker_compacts_repeatedly_and_finishes_without_replaying_writes(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._control["task_workers_context_chars"] = 14000
    shell.execute.side_effect = [
        'Saved /storage/report.txt\n' + '\\"雪' * 20000,
        *['Source findings: ' + '\\"雪' * 20000] * 39,
    ]
    rounds = 0
    checkpoints = 0
    resumed = False

    async def generate(messages, **kwargs):
        nonlocal rounds, checkpoints, resumed
        # The schema/transport reserve is taken out before message budgeting.
        assert len(json.dumps(messages)) < 14000 - 2048
        if kwargs["tools"]:
            pending = set()
            for row in messages:
                if row["role"] == "assistant":
                    assert not pending
                    pending = {item["id"] for item in row.get("tool_calls", [])}
                elif row["role"] == "tool":
                    pending.remove(row["tool_call_id"])
            assert not pending
        if not kwargs["tools"]:
            checkpoints += 1
            assert "/storage/report.txt" in json.dumps(messages)
            return ProviderResult(
                "Completed: wrote /storage/report.txt; do not write it again. "
                "Continue gathering sources, then report /storage/report.txt."
            )
        if checkpoints:
            resumed = True
            assert any(
                "Working memory" in row.get("content", "")
                and "/storage/report.txt" in row["content"]
                for row in messages
            )
        rounds += 1
        if rounds == 41:
            return ProviderResult("Report ready: /storage/report.txt")
        return call(command="write-report" if rounds == 1 else f"read-source-{rounds}")

    bot._generate_response.side_effect = generate
    report = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert report["status"] == "reported"
    assert report["report"] == "Report ready: /storage/report.txt"
    assert checkpoints > 1 and resumed
    commands = [c.kwargs["command"] for c in shell.execute.await_args_list]
    assert commands == ["write-report"] + [f"read-source-{i}" for i in range(2, 41)]
    assert report["completed"] == [{"tool": "shell", "status": "returned"}] * 40


@pytest.mark.parametrize("invalid", [ProviderResult(""), call(), ProviderResult("x" * 20000)])
def test_invalid_checkpoint_stops_without_replaying_completed_work(tmp_path, invalid):
    bot, msg, shell, worker = setup(tmp_path)
    bot._control["task_workers_context_chars"] = 14000
    shell.execute.return_value = "large output " * 10000

    async def generate(messages, **kwargs):
        return call() if kwargs["tools"] else invalid

    bot._generate_response.side_effect = generate
    report = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert report["status"] == "failed"
    assert report["completed"]
    assert len(report["completed"]) == shell.execute.await_count
    assert bot._generate_response.await_args.kwargs["tools"] == []


def test_compaction_rechecks_entitlement_between_fragments(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    base = [{"role": "user", "content": "Research without repeating writes"}]
    chunks = [[{"role": "tool", "content": "雪\\\"" * 10000}]]

    async def generate(messages, **kwargs):
        bot._message_quota.configure("123", plan="free")
        return ProviderResult("Completed write: /storage/report.txt")

    bot._generate_response.side_effect = generate
    with pytest.raises(PermissionError, match="invalidated"):
        asyncio.run(worker._compact_context(msg, base, chunks, 8000, None, bot.ai_provider, 4096))
    shell.execute.assert_not_awaited()
    assert bot._generate_response.await_count == 1


@pytest.mark.parametrize("configured", [None, 999])
def test_worker_allows_200_rounds_but_stops_before_201(tmp_path, configured):
    bot, msg, shell, worker = setup(tmp_path)
    if configured is not None:
        bot._control["task_workers_max_steps"] = configured
    bot._generate_response.side_effect = None
    bot._generate_response.return_value = call()
    report = json.loads(asyncio.run(worker.run(msg, tasks=TASKS)))[0]
    assert report["status"] == "step_limit"
    assert shell.execute.await_count == 200
    assert report["completed"] == [{"tool": "shell", "status": "returned"}] * 200
