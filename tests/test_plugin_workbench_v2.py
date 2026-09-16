import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from plugins.maxwell_extras import developer
from plugins.maxwell_extras import workbench_v2


class _Manager:
    def __init__(self, name):
        self.name = name
        self.loaded_plugins = {name: {"tools": {}, "module": SimpleNamespace(), "context": None}}
        self.reloads = 0
        self._job_specs = {}

    def reload_plugins(self):
        self.reloads += 1
        self.loaded_plugins[self.name] = {
            "tools": {},
            "module": SimpleNamespace(),
            "context": None,
        }
        return "Reloaded"

    def plugin_events(self, _name):
        return []


class _Ctx:
    def __init__(self, data):
        self.data = Path(data)

    def store_path(self, name):
        return self.data / name


def _bot(name):
    bot = SimpleNamespace()
    bot._is_admin = lambda uid: uid == 1
    bot.plugin_manager = _Manager(name)
    bot.tools = {}
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
    (p / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": "demo plugin",
                "version": "1.0.0",
                "enabled_globally": True,
            }
        )
    )
    (p / "__init__.py").write_text("def setup(bot, ctx):\n    return []\n")
    (p / "tools.py").write_text("VALUE = 1\n")
    return p


def test_scaffold_is_a_pending_proposal_and_can_create_plugin(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    bot = _bot("fresh")
    tool = workbench_v2.PluginWorkbenchTool(bot, _Ctx(tmp_path / "data"))

    async def run():
        created = await tool.execute(
            _msg(10),
            action="scaffold",
            plugin_name="fresh",
            description="A fresh test plugin",
            template="full",
        )
        assert "Waiting for explicit admin approval" in created
        assert not (plugins / "fresh" / "plugin.json").exists()
        pid = (await tool.store.pending())[0]["id"]
        same = await tool.execute(_msg(10), action="apply", proposal_id=pid)
        assert "later user turn" in same
        applied = await tool.execute(_msg(11), action="apply", proposal_id=pid)
        assert "hot-reloaded" in applied
        root = plugins / "fresh"
        assert (root / "plugin.json").is_file()
        assert (root / "__init__.py").is_file()
        assert (root / "tools.py").is_file()
        assert "Revision snapshot recorded" in applied

    asyncio.run(run())


def test_replace_records_revision_and_rollback_creates_new_proposal(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugin = _make_plugin(plugins)
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    bot = _bot("demo")
    tool = workbench_v2.PluginWorkbenchTool(bot, _Ctx(tmp_path / "data"))

    async def run():
        proposed = await tool.execute(
            _msg(20),
            action="replace",
            plugin_name="demo",
            summary="replace tools implementation",
            files=[{"path": "tools.py", "content": "VALUE = 2\n"}],
        )
        assert "Waiting for explicit admin approval" in proposed
        pid = (await tool.store.pending())[0]["id"]
        result = await tool.execute(_msg(21), action="apply", proposal_id=pid)
        assert "Revision snapshot recorded" in result
        assert (plugin / "tools.py").read_text() == "VALUE = 2\n"

        history = await tool.execute(_msg(22), action="history", plugin_name="demo")
        assert pid in history
        rollback = await tool.execute(
            _msg(23),
            action="rollback",
            revision_id=pid,
        )
        assert "Waiting for explicit admin approval" in rollback
        pending = await tool.store.pending()
        rollback_id = pending[0]["id"]
        assert rollback_id != pid
        applied_rollback = await tool.execute(
            _msg(24), action="apply", proposal_id=rollback_id
        )
        assert "hot-reloaded" in applied_rollback
        assert (plugin / "tools.py").read_text() == "VALUE = 1\n"

    asyncio.run(run())


def test_doctor_compiles_files_and_reports_broken_python(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugin = _make_plugin(plugins)
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    tool = workbench_v2.PluginWorkbenchTool(_bot("demo"), _Ctx(tmp_path / "data"))

    healthy = asyncio.run(tool.execute(_msg(), action="doctor", plugin_name="demo"))
    assert "PASS" in healthy

    (plugin / "tools.py").write_text("def broken(:\n")
    broken = asyncio.run(tool.execute(_msg(), action="doctor", plugin_name="demo"))
    assert "FAIL" in broken
    assert "tools.py" in broken


def test_guide_and_tree_expose_supported_authoring_surface(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    _make_plugin(plugins)
    monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
    tool = workbench_v2.PluginWorkbenchTool(_bot("demo"), _Ctx(tmp_path / "data"))

    guide = asyncio.run(tool.execute(_msg(), action="guide"))
    assert "ctx.on_event" in guide
    assert "ctx.every" in guide
    tree = asyncio.run(tool.execute(_msg(), action="tree", plugin_name="demo"))
    assert "plugin.json" in tree
    assert "tools.py" in tree
