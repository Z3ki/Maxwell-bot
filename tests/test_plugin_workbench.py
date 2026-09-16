import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from plugins.maxwell_extras import developer


class _Manager:
    def __init__(self, name):
        self.name = name
        self.loaded_plugins = {name: {}}
        self.reloads = 0

    def reload_plugins(self):
        self.reloads += 1
        self.loaded_plugins[self.name] = {}
        return "Reloaded"


class _Ctx:
    def __init__(self, data):
        self.data = Path(data)

    def store_path(self, name):
        return self.data / name


def _bot(name):
    bot = SimpleNamespace()
    bot._is_admin = lambda uid: uid == 1
    bot.plugin_manager = _Manager(name)
    return bot


def _msg(mid=1, uid=1, content="accept it"):
    return SimpleNamespace(
        id=mid,
        content=content,
        author=SimpleNamespace(id=uid),
        channel=SimpleNamespace(id=50),
    )


def _make_plugin(root: Path, name="demo"):
    p = root / name
    p.mkdir(parents=True)
    (p / "plugin.json").write_text(json.dumps({"name": name, "enabled_globally": True}))
    (p / "__init__.py").write_text("def setup(bot, ctx):\n    return []\n")
    (p / "tools.py").write_text("VALUE = 1\n")
    return p


def test_workbench_requires_later_turn_and_hot_reloads(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugin = _make_plugin(plugins)
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    bot = _bot("demo")
    tool = developer.PluginWorkbenchTool(bot, _Ctx(tmp_path / "data"))

    async def run():
        created = await tool.execute(
            _msg(10),
            action="propose",
            plugin_name="demo",
            summary="increment value",
            edits=[{"path": "tools.py", "old": "VALUE = 1", "new": "VALUE = 2"}],
        )
        pending = await tool.store.pending()
        assert "Waiting for explicit admin approval" in created
        assert len(pending) == 1
        pid = pending[0]["id"]
        same_turn = await tool.execute(_msg(10), action="apply", proposal_id=pid)
        assert "later user turn" in same_turn
        assert (plugin / "tools.py").read_text() == "VALUE = 1\n"
        applied = await tool.execute(_msg(11), action="apply", proposal_id=pid)
        assert "hot-reloaded" in applied
        assert (plugin / "tools.py").read_text() == "VALUE = 2\n"
        assert bot.plugin_manager.reloads == 1

    asyncio.run(run())


def test_workbench_refuses_stale_proposal(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugin = _make_plugin(plugins)
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    bot = _bot("demo")
    tool = developer.PluginWorkbenchTool(bot, _Ctx(tmp_path / "data"))

    async def run():
        await tool.execute(
            _msg(20),
            action="propose",
            plugin_name="demo",
            edits=[{"path": "tools.py", "old": "VALUE = 1", "new": "VALUE = 2"}],
        )
        pid = (await tool.store.pending())[0]["id"]
        (plugin / "tools.py").write_text("VALUE = 9\n")
        result = await tool.execute(_msg(21), action="apply", proposal_id=pid)
        assert "no longer applies cleanly" in result or "changed since" in result
        assert (plugin / "tools.py").read_text() == "VALUE = 9\n"

    asyncio.run(run())


def test_workbench_admin_only_and_path_scoped(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    _make_plugin(plugins)
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    tool = developer.PluginWorkbenchTool(_bot("demo"), _Ctx(tmp_path / "data"))

    async def run():
        denied = await tool.execute(_msg(uid=2), action="list")
        assert denied.startswith("Error:")
        bad = await tool.execute(
            _msg(),
            action="propose",
            plugin_name="demo",
            edits=[{"path": "../bot.py", "old": "a", "new": "b"}],
        )
        assert "invalid plugin proposal" in bad

    asyncio.run(run())


def test_workbench_later_turn_still_requires_explicit_approval(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugin = _make_plugin(plugins)
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    bot = _bot("demo")
    tool = developer.PluginWorkbenchTool(bot, _Ctx(tmp_path / "data"))

    async def run():
        await tool.execute(
            _msg(30),
            action="propose",
            plugin_name="demo",
            edits=[{"path": "tools.py", "old": "VALUE = 1", "new": "VALUE = 2"}],
        )
        pid = (await tool.store.pending())[0]["id"]
        result = await tool.execute(
            _msg(31, content="show me the proposal again"),
            action="apply",
            proposal_id=pid,
        )
        assert "explicitly approve" in result
        assert (plugin / "tools.py").read_text() == "VALUE = 1\n"

    asyncio.run(run())
