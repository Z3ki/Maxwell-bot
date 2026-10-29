"""Presentation snapshots do not perform synchronous DB reads in the event loop."""
import asyncio
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot import MaxwellBot
from premium_access import (
    current_plan,
    load_plan,
    premium_for_current_request,
    premium_withheld_note,
)


def test_plan_read_runs_in_worker_thread_and_fails_closed():
    main = threading.get_ident()
    def read(uid):
        assert threading.get_ident() != main
        assert uid == "123"
        return "premium"
    bot = SimpleNamespace(_message_quota=SimpleNamespace(plan_for=read))
    assert asyncio.run(load_plan(bot, "123")) == "premium"
    bot._message_quota.plan_for = lambda uid: (_ for _ in ()).throw(OSError("unavailable"))
    assert asyncio.run(load_plan(bot, "123")) == "free"


def test_request_snapshot_isolated_by_user_and_reused_by_provider():
    def forbidden_read(uid):
        raise AssertionError("snapshot should avoid another DB lookup")
    reader = Mock(side_effect=forbidden_read)
    bot = SimpleNamespace(
        _message_quota=SimpleNamespace(plan_for=reader),
        _control={}, config=SimpleNamespace(AI_PREMIUM_MODEL="slow-reasoning-model"),
        ai_provider=SimpleNamespace(generate_response=AsyncMock(return_value="done")),
        _night_fallback_kwargs=dict,
    )
    bot._wants_premium_model = MethodType(MaxwellBot._wants_premium_model, bot)
    token = current_plan.set(("123", "premium"))
    try:
        assert premium_for_current_request("123")
        assert not premium_for_current_request("456")
        assert asyncio.run(MaxwellBot._generate_response(bot, [], quota_user_id="123")) == "done"
    finally:
        current_plan.reset(token)
    assert not premium_for_current_request("123")
    reader.assert_not_called()
    assert bot.ai_provider.generate_response.await_args.kwargs["model"] == "slow-reasoning-model"


def test_free_turn_is_told_why_the_subagent_is_missing():
    bot = SimpleNamespace(
        tools={"delegate_task": object()},
        _control={"subagents_require_premium": True},
    )
    note = premium_withheld_note(bot, {"shell"}, "123")
    assert "Premium" in note and "not in this turn" in note
    assert "restart" in note
    token = current_plan.set(("123", "premium"))
    try:
        assert premium_withheld_note(bot, set(), "123") == ""
    finally:
        current_plan.reset(token)
    assert premium_withheld_note(bot, {"delegate_task"}, "123") == ""
    bot._control["subagents_require_premium"] = False
    assert premium_withheld_note(bot, {"shell"}, "123") == ""


def test_open_gate_offers_subagents_and_keeps_the_other_premium_tools():
    bot = SimpleNamespace(
        tools={
            "delegate_task": object(),
            "schedule": object(),
            "storage": object(),
            "shell": object(),
        },
        _control={},
        plugin_manager=None,
        _is_admin=lambda uid: False,
    )
    message = SimpleNamespace(author=SimpleNamespace(id=123), guild=None)
    names = MaxwellBot._turn_tool_names(bot, "discord", message)
    assert "delegate_task" in names and "shell" in names
    assert "schedule" not in names and "storage" not in names
    bot._control["subagents_require_premium"] = True
    hidden = MaxwellBot._turn_tool_names(bot, "discord", message)
    assert "delegate_task" not in hidden
    bot._control = {"subagents_require_premium": False, "premium_task_workers_enabled": False}
    dark = MaxwellBot._turn_tool_names(bot, "discord", message)
    assert "delegate_task" not in dark and "schedule" not in dark
