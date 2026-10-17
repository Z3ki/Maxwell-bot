"""Custom instructions and model-chosen permissions remain user scoped and revocable."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.maxwell_extras.task_agents import ManageTaskAgentTool, TaskAgentStore
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from privacy_data import cleanup_data, delete_data
from test_premium_task_workers import setup, call


def store(root):
    return TaskAgentStore(root / "plugins/maxwell_extras/task_agents.sqlite3")


def test_profiles_persist_owner_scope_and_model_selected_tool_permissions(tmp_path):
    saved = store(tmp_path)
    saved.save(
        "123",
        "builder",
        "custom",
        "Build accessible pages",
        tools=["shell", "edit_site"],
    )
    fresh = store(tmp_path)
    assert fresh.get("123", "builder")["tools"] == ["edit_site", "shell"]
    assert fresh.list("456") == []
    assert fresh.delete("456", "builder") == 0
    assert fresh.get("123", "builder")["instructions"] == "Build accessible pages"


@pytest.mark.parametrize(
    "name,kind,tools",
    [
        ("../bad", "custom", ["shell"]),
        ("bad", "admin", ["shell"]),
        ("bad", "custom", ["manage_plugin"]),
        ("bad", "custom", ["delegate_task"]),
        ("bad", "research", ["shell"]),
        ("bad", "custom", []),
    ],
)
def test_profiles_cannot_grant_privileged_or_out_of_scope_tools(
    tmp_path, name, kind, tools
):
    with pytest.raises(ValueError):
        store(tmp_path).save("123", name, kind, "Instructions", tools=tools)


def test_concurrent_profile_limit_is_atomic(tmp_path):
    saved = store(tmp_path)

    def create(number):
        try:
            saved.save("123", f"worker-{number}", "shell", "Inspect workspace")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=6) as workers:
        assert sum(workers.map(create, range(15))) == 10
    assert len(saved.list("123")) == 10


def test_expiry_and_full_deletion_prevent_stale_profile_writes(tmp_path):
    saved = store(tmp_path)
    preferences = UserPreferenceStore(
        tmp_path / "plugins/maxwell_extras/user_preferences.json"
    )
    saved.save("123", "expired", "shell", "Old instructions")
    with saved.db() as db:
        db.execute("UPDATE agents SET updated_at=?", (time.time() - 91 * 86400,))
    assert saved.list("123") == []
    cleanup_data(tmp_path, preferences)
    with saved.db() as db:
        assert db.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
    saved.save("123", "current", "shell", "New instructions")
    started = time.time()
    result = delete_data(tmp_path, "123", preferences)
    assert result["deleted"]["custom_agents"] == 1
    with pytest.raises(ValueError, match="predates"):
        saved.save("123", "current", "shell", "Stale instructions", started_at=started)
    assert saved.list("123") == []


def test_maxwell_can_save_and_change_worker_permissions_without_changing_caller(
    tmp_path,
):
    bot, msg, shell, worker = setup(tmp_path)
    bot._task_agents = store(tmp_path)
    manager = ManageTaskAgentTool(bot)

    async def run():
        assert "Saved" in await manager.execute(
            msg,
            action="save",
            name="builder",
            kind="custom",
            instructions="Verify the result",
            tools=["shell", "edit_site"],
        )
        assert "Saved" in await manager.execute(
            msg,
            action="save",
            name="builder",
            kind="custom",
            instructions="Review only",
            tools=["list_sites"],
        )
        assert (await manager.execute(msg, action="list"))[0]["tools"] == ["list_sites"]

    asyncio.run(run())
    assert set(bot.tools) == {"shell"} and not bot._is_admin("123")
    assert bot._message_quota.plan_for("123") == "premium"


def test_free_users_can_review_and_delete_profiles_but_cannot_save(tmp_path):
    bot, msg, shell, worker = setup(tmp_path, premium=False)
    bot._task_agents = store(tmp_path)
    bot._task_agents.save("123", "old", "shell", "Inspect")
    manager = ManageTaskAgentTool(bot)

    async def run():
        assert "requires Premium" in await manager.execute(
            msg, action="save", name="new", kind="shell", instructions="Inspect"
        )
        assert len(await manager.execute(msg, action="list")) == 1
        assert await manager.execute(msg, action="delete", name="old") == {"deleted": 1}

    asyncio.run(run())


def test_worker_loads_only_owners_profile_and_enforces_model_chosen_permissions(
    tmp_path,
):
    bot, msg, shell, worker = setup(tmp_path)
    bot._task_agents = store(tmp_path)
    bot._task_agents.save(
        "123", "builder", "custom", "Inspect accessible assets", tools=["shell"]
    )
    tasks = [{"profile": "builder", "task": "Verify local files"}]
    result = json.loads(asyncio.run(worker.execute(msg, tasks=tasks)))[0]
    assert result["status"] == "reported"
    prompt = bot._generate_response.await_args_list[0].args[0]
    assert "Inspect accessible assets" in str(prompt)
    assert [
        s["function"]["name"]
        for s in bot._generate_response.await_args_list[0].kwargs["tools"]
    ] == ["shell"]
    bot._task_agents.delete("123", "builder")
    bot._task_agents.save(
        "456",
        "builder",
        "custom",
        "Another user's secret instructions",
        tools=["shell"],
    )
    bot._generate_response.reset_mock()
    assert "not found for this user" in asyncio.run(worker.execute(msg, tasks=tasks))
    bot._generate_response.assert_not_awaited()


def test_per_task_permissions_only_narrow_profile_and_never_add_tools(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._task_agents = store(tmp_path)
    bot._task_agents.save("123", "builder", "custom", "Inspect", tools=["list_sites"])
    result = json.loads(
        asyncio.run(
            worker.execute(
                msg,
                tasks=[{"profile": "builder", "task": "Inspect", "tools": ["shell"]}],
            )
        )
    )[0]
    assert result["status"] == "unavailable"
    shell.execute.assert_not_awaited()


def test_permissions_removed_during_model_call_stop_before_execution(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    bot._task_agents = store(tmp_path)
    bot._task_agents.save("123", "builder", "custom", "Inspect", tools=["shell"])

    async def generate(*args, **kwargs):
        bot._task_agents.save(
            "123", "builder", "custom", "Review", tools=["list_sites"]
        )
        return call()

    bot._generate_response.side_effect = generate
    result = json.loads(
        asyncio.run(
            worker.execute(msg, tasks=[{"profile": "builder", "task": "Inspect"}])
        )
    )[0]
    assert result["status"] == "failed"
    shell.execute.assert_not_awaited()


def test_custom_transient_permissions_work_without_saving_profile(tmp_path):
    bot, msg, shell, worker = setup(tmp_path)
    result = json.loads(
        asyncio.run(
            worker.execute(
                msg, tasks=[{"kind": "custom", "tools": ["shell"], "task": "Inspect"}]
            )
        )
    )[0]
    assert result["status"] == "reported"
    assert not (tmp_path / "plugins").exists()
