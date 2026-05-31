import asyncio
import json
from types import SimpleNamespace

from autonomy import (
    AUTONOMY_DISABLED_TOOLS,
    AutonomyEngine,
    AutonomyStore,
    _truncate,
)


class DummyTool:
    def get_description(self):
        return "dummy"

    async def execute(self, *args, **kwargs):  # pragma: no cover - should not be hit
        raise AssertionError("disabled tool executed")


def _engine(tmp_path, *, auto_channels=None, tools=None):
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels=set(auto_channels or []),
        tools=tools or {},
    )
    return AutonomyEngine(bot)


def test_parse_plan_fallback_channel_uses_first_auto_channel(tmp_path):
    engine = _engine(tmp_path, auto_channels={"100", "200"})

    raw = json.dumps({
        "thought": "say something",
        "actions": [{"kind": "post_channel", "content": "hello"}],
    })
    actions, failures = engine._parse_plan(raw)

    assert failures == []
    assert actions == [{
        "kind": "post_channel",
        "target_channel_id": "100",
        "content": "hello",
        "reason": "",
    }]


def test_exec_run_tool_refuses_disabled_tools(tmp_path):
    disabled = next(iter(AUTONOMY_DISABLED_TOOLS))
    engine = _engine(tmp_path, auto_channels={"100"}, tools={disabled: DummyTool()})
    result = {"kind": "run_tool", "result": "success", "error": None}

    asyncio.run(engine._exec_run_tool({"tool_name": disabled, "tool_args": {}}, result))

    assert result["result"] == "error"
    assert "disabled" in result["error"]


def test_create_goal_reports_store_limit_as_error(tmp_path):
    engine = _engine(tmp_path)
    engine.store = AutonomyStore(str(tmp_path))

    async def run():
        for idx in range(engine.store.MAX_GOALS):
            await engine.store.add_goal(f"goal {idx}")
        result = {"kind": "create_goal", "result": "success", "error": None}
        await engine._exec_create_goal({"description": "one too many"}, result)
        return result

    result = asyncio.run(run())
    assert result["result"] == "error"
    assert result["error"] == "goal limit reached"
    assert result["goal_id"] is None


def test_truncate_handles_tiny_budgets():
    assert _truncate("abcdef", 0) == ""
    assert _truncate("abcdef", 3) == "abc"
    assert _truncate("abcdef", 99) == "abcdef"
