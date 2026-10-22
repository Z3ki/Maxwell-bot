import asyncio
import json
from types import SimpleNamespace

import jsonschema
import pytest

import provider_limits
from api.state import _sanitize_control
from bot import _current_request_provider
from config import Config, _int_env
from control_defaults import DEFAULT_CONTROL
from maxwell_core.providers.models import ProviderResult
from plugins.maxwell_extras.task_workers import DelegateTaskTool
from provider_limits import (
    WORKER_TIMEOUT_DEFAULT_SECONDS,
    WORKER_TIMEOUT_MAX_SECONDS,
    request_timeout_seconds,
    requested_worker_timeout_seconds,
    worker_job_timeout_seconds,
    worker_timeout_seconds,
)
from test_detached_task_jobs import environment, join
from test_premium_task_workers import TASKS, call, setup


def _bot(**control):
    return SimpleNamespace(
        config=SimpleNamespace(AI_WORKER_TURN_TIMEOUT_SECONDS=0), _control=control
    )


def test_default_worker_budget_is_120_minutes_from_control_defaults():
    assert DEFAULT_CONTROL["task_workers_timeout_seconds"] == 7200
    assert WORKER_TIMEOUT_DEFAULT_SECONDS == 7200
    assert worker_timeout_seconds(_bot()) == 7200
    assert worker_job_timeout_seconds(_bot()) == 7200


def test_model_can_request_up_to_240_minutes():
    assert WORKER_TIMEOUT_MAX_SECONDS == 14400
    assert requested_worker_timeout_seconds(7200, None) == 7200
    assert requested_worker_timeout_seconds(7200, 25) == 1500
    assert requested_worker_timeout_seconds(7200, 180) == 10800
    assert requested_worker_timeout_seconds(7200, 240) == 14400
    assert requested_worker_timeout_seconds(7200, 900) == 14400
    assert requested_worker_timeout_seconds(7200, 5) == 300
    assert requested_worker_timeout_seconds(7200, "bad") == 7200
    assert requested_worker_timeout_seconds(7200, 0) == 7200


def test_operator_controls_reach_four_hours():
    assert worker_timeout_seconds(_bot(task_workers_timeout_seconds=14400)) == 14400
    assert worker_timeout_seconds(_bot(task_workers_timeout_seconds=99999)) == 14400
    env = SimpleNamespace(AI_WORKER_TURN_TIMEOUT_SECONDS=14400)
    assert worker_timeout_seconds(SimpleNamespace(config=env, _control={})) == 14400
    assert worker_job_timeout_seconds(_bot(task_workers_timeout_seconds=14400), 240) == 14400
    assert request_timeout_seconds(_bot(ai_timeout_seconds=14400)) == 14400


def test_saved_control_sanitizer_allows_four_hours():
    out = _sanitize_control(
        {
            "task_workers_timeout_seconds": 14400,
            "ai_timeout_seconds": 14400,
            "live_turn_timeout_seconds": 14400,
            "tool_iteration_timeout_seconds": 14400,
        }
    )
    assert out["task_workers_timeout_seconds"] == 14400
    assert out["ai_timeout_seconds"] == 14400
    assert out["live_turn_timeout_seconds"] == 14400
    assert out["tool_iteration_timeout_seconds"] == 14400
    assert _sanitize_control({"task_workers_timeout_seconds": 10**9})[
        "task_workers_timeout_seconds"
    ] == 14400
    assert _sanitize_control({})["task_workers_timeout_seconds"] == 7200


@pytest.mark.parametrize(
    "name",
    [
        "AI_WORKER_REQUEST_TIMEOUT_SECONDS",
        "AI_WORKER_TURN_TIMEOUT_SECONDS",
        "AI_REQUEST_TIMEOUT_SECONDS",
        "AI_TURN_TIMEOUT_SECONDS",
    ],
)
def test_env_timeouts_accept_four_hours(monkeypatch, name):
    monkeypatch.setenv(name, "14400")
    assert _int_env(name, 0, min_value=0, max_value=14400) == 14400
    # The live Config class must use the same ceiling.
    import inspect

    source = inspect.getsource(Config)
    line = next(row for row in source.splitlines() if f'_int_env("{name}"' in row)
    assert "max_value=14400" in line


def test_schema_accepts_timeout_minutes_up_to_240():
    schema = DelegateTaskTool.parameters
    jsonschema.validate({"tasks": [{"task": "x"}], "timeout_minutes": 240}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"tasks": [{"task": "x"}], "timeout_minutes": 241}, schema)


def test_tool_description_advertises_new_budget(tmp_path):
    _, _, _, worker = setup(tmp_path)
    text = worker.get_description()
    assert "120 minutes" in text and "240" in text
    assert "30 for" not in text
    assert "240" in DelegateTaskTool.parameters["properties"]["timeout_minutes"]["description"]


@pytest.mark.parametrize("minutes, expected", [(None, 7200), (240, 14400)])
def test_chat_byok_whole_task_deadline_does_not_cap_subagent(
    tmp_path, monkeypatch, minutes, expected
):
    bot, msg, _, worker = setup(tmp_path)
    seen = []
    real = provider_limits.worker_job_timeout_seconds

    def spy(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(
        "plugins.maxwell_extras.task_workers.worker_job_timeout_seconds", spy
    )
    # A personal chat connection with the default 15 minute chat-turn deadline.
    provider = SimpleNamespace(
        policy=SimpleNamespace(max_request_seconds=300),
        turn_timeout_seconds=900,
        capabilities=SimpleNamespace(context_window=32000),
        max_tokens=2000,
    )
    bot._generate_response.side_effect = [ProviderResult("done")]
    token = _current_request_provider.set(provider)
    try:
        asyncio.run(worker.run(msg, tasks=TASKS, timeout_minutes=minutes))
    finally:
        _current_request_provider.reset(token)
    assert seen == [expected]
    # Per-call request timeout still follows the connection.
    assert bot._generate_response.await_args.kwargs["timeout"] == 300


def test_parent_turn_timeout_does_not_cancel_background_subagent(tmp_path):
    async def run():
        bot, msg, shell, engine, manager = environment(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(messages, **kwargs):
            entered.set()
            await release.wait()
            used_tool = any(row.get("role") == "tool" for row in messages)
            return call() if not used_tool else ProviderResult("Verified worker result")

        bot._generate_response.side_effect = generate
        receipt = {}

        async def parent_turn():
            receipt.update(json.loads(await engine.execute(msg, tasks=TASKS)))
            await asyncio.Event().wait()

        try:
            # Stand-in for the parent chat turn / tool-iteration deadline expiring.
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await parent_turn()
            job = manager.jobs[receipt["id"]]
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0.1)
            assert not job.task.done() and job.row["state"] == "running"
            release.set()
            await join(job)
            assert job.row["state"] == "completed"
        finally:
            release.set()
            await manager.close()

    asyncio.run(run())
