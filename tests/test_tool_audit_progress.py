import asyncio
from types import SimpleNamespace

from plugins.maxwell_extras import audit_ui
from plugins.maxwell_extras.audit_progress_fix import install_progress_audit_fix
from tool_progress import ToolProgress


class _Posted:
    def __init__(self):
        self.id = 700
        self.view = None

    async def edit(self, **kwargs):
        self.view = kwargs.get("view", self.view)


def test_progress_transition_keeps_tool_trace_button(tmp_path, monkeypatch):
    msg = SimpleNamespace(id=123, channel=SimpleNamespace(id=5), tool_platform="discord")
    posted = _Posted()
    progress = ToolProgress(msg)
    progress._posted = posted

    async def clearing_transition(self, content):
        self._posted = None
        return True

    monkeypatch.setattr(ToolProgress, "transition_to_final", clearing_transition)
    store = audit_ui.ToolAuditStore(tmp_path / "traces.json")
    bot = SimpleNamespace(_maxwell_tool_audit_store=store)
    audit_ui._MESSAGE_BOTS["123"] = bot
    install_progress_audit_fix(bot)

    async def run():
        await store.record(msg, {"name": "web_search", "source": "auto"})
        ok = await progress.transition_to_final("done")
        assert ok is True
        assert posted.view is not None
        assert posted.view.children[0].label == "Tools · 1"

    asyncio.run(run())
