import asyncio
import types
from pathlib import Path

from plugins.agent_life.impl import AgentLifeService, AgentLifeTool, _safe_rel


class Ctx:
    def __init__(self, root): self.data_dir = Path(root)


class Bot:
    bg_jobs = None
    autonomy_engine = None


def test_safe_rel_rejects_escape():
    assert _safe_rel("repo/src") == "repo/src"
    for bad in ("/etc", "..", "../x", "a/../../b"):
        try:
            _safe_rel(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_task_owner_isolation(tmp_path):
    async def run():
        svc = AgentLifeService(Bot(), Ctx(tmp_path))
        await svc.put_task("a", {"user_id":"1","channel_id":"10","kind":"work","goal":"x","enabled":True,"next_run_at":9999999999,"interval_seconds":0})
        await svc.put_task("b", {"user_id":"2","channel_id":"20","kind":"work","goal":"y","enabled":True,"next_run_at":9999999999,"interval_seconds":0})
        assert [x["id"] for x in await svc.tasks_for("1")] == ["a"]
        assert not await svc.remove_task("1", "b")
        assert await svc.remove_task("1", "a")
    asyncio.run(run())


def test_remind_belongs_to_caller(tmp_path):
    async def run():
        svc = AgentLifeService(Bot(), Ctx(tmp_path))
        tool = AgentLifeTool(Bot(), svc)
        msg = types.SimpleNamespace(
            author=types.SimpleNamespace(id="1"),
            channel=types.SimpleNamespace(id="10"),
        )
        out = await tool.execute(msg, action="remind", text="check deploy", after_seconds=99999)
        assert out.startswith("saved ")
        rows = await svc.tasks_for("1")
        assert len(rows) == 1
        assert rows[0]["kind"] == "reminder"
        other = types.SimpleNamespace(
            author=types.SimpleNamespace(id="2"),
            channel=types.SimpleNamespace(id="20"),
        )
        assert await tool.execute(other, action="cancel", task_id=rows[0]["id"]) == "task not found"
        assert await tool.execute(msg, action="cancel", task_id=rows[0]["id"]) == "cancelled"
    asyncio.run(run())
