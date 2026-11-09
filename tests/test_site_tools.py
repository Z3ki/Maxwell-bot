"""create_site / edit_site / delete_site: multi-file sites, patching, backend.

The old tool could do exactly one thing — write index.html and never touch it
again. These cover what replaced that: extra files, in-place edits, a
server-side store, per-site lifetime, and the ownership checks that all of it
has to keep honouring.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import site_backend
from bot_tools import CreateSiteTool, DeleteSiteTool, EditSiteTool, ListSitesTool


@pytest.fixture
def bot(tmp_path):
    site_dir = tmp_path / "public" / "bot"
    data_dir = tmp_path / "data"
    site_dir.mkdir(parents=True)
    data_dir.mkdir()
    control = {}
    return SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_SITE_DIR=str(site_dir),
            MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.com",
            DATA_DIR=str(data_dir),
        ),
        _sites={},
        _load_sites=lambda quiet=True: None,
        _is_admin=lambda _uid: False,
        _control=control,
        control=control,
        tools={},
    )


def _msg(uid=42, name="tester"):
    return SimpleNamespace(author=SimpleNamespace(id=uid, display_name=name))


def run(coro):
    return asyncio.run(coro)


PAGE = "<!DOCTYPE html><html><head><title>t</title></head><body><h1>hi</h1></body></html>"


def test_host_file_maximal_slug_never_overwrites_existing_file(bot):
    from plugins.sites.impl import HostFileTool
    from urllib.parse import urlparse

    tool = HostFileTool(bot)
    name = "a" * 30
    first = run(tool.execute(_msg(42), name=name, filename="report.txt", content="first"))
    second = run(tool.execute(_msg(43), name=name, filename="report.txt", content="second"))
    first_url = first.splitlines()[0].removeprefix("Hosted file: ")
    second_url = second.splitlines()[0].removeprefix("Hosted file: ")
    assert first_url != second_url
    root = Path(bot.config.MAXWELL_SITE_DIR) / "_files"
    assert (root / name / "report.txt").read_text() == "first"
    second_slug = urlparse(second_url).path.split("/")[-2]
    assert len(second_slug) <= 30
    assert (root / second_slug / "report.txt").read_text() == "second"


@pytest.mark.parametrize("tool_name", ["edit", "delete", "server"])
def test_queued_site_action_rechecks_reclaimed_owner(bot, monkeypatch, tool_name):
    import site_server
    from plugins.sites.impl import SiteServerTool
    from utils import FileLock

    async def no_backend(*_args, **_kwargs):
        return None

    monkeypatch.setattr(site_server, "start", no_backend)
    monkeypatch.setattr(site_server, "destroy", no_backend)

    run(CreateSiteTool(bot).execute(_msg(42), name="reclaimed", title="Old", body=PAGE))
    tool = {"edit": EditSiteTool, "delete": DeleteSiteTool, "server": SiteServerTool}[tool_name](bot)
    original_resolve = tool._resolve
    root = Path(bot.config.MAXWELL_SITE_DIR) / "reclaimed"

    async def scenario():
        resolved = asyncio.Event()

        def resolve(message, name):
            result = original_resolve(message, name)
            resolved.set()
            return result

        monkeypatch.setattr(tool, "_resolve", resolve)
        with FileLock(Path(bot.config.DATA_DIR) / "site_locks/reclaimed.lock", timeout=0):
            kwargs = {"name": "reclaimed"}
            if tool_name == "edit":
                kwargs.update(action="write", content="old owner overwrite")
            elif tool_name == "server":
                kwargs.update(action="write", content="old owner code")
            task = asyncio.create_task(tool.execute(_msg(42), **kwargs))
            await resolved.wait()
            # Model a deletion/reclaim completed by the current lock holder.
            bot._sites["reclaimed"] = dict(bot._sites["reclaimed"], user_id="43")
            root.mkdir(exist_ok=True)
            (root / "index.html").write_text("new owner page")
            await asyncio.sleep(0)
        result = await task
        assert result.startswith("Error:")
        assert "belongs" in result or "only the site owner" in result
        assert (root / "index.html").read_text() == "new owner page"
        assert bot._sites["reclaimed"]["user_id"] == "43"

    run(scenario())


def test_reused_site_slug_cannot_inherit_previous_store(bot, monkeypatch):
    calls = []
    monkeypatch.setattr("site_backend.destroy", lambda *_args: calls.append("store") or (_ for _ in ()).throw(TimeoutError("store busy")))
    tool = CreateSiteTool(bot)
    first = run(tool.execute(_msg(42), name="reused", title="Old", body=PAGE, backend=True))
    assert "https://" in first
    second = run(tool.execute(_msg(42), name="reused", title="New", body=PAGE))
    assert second.startswith("Error creating site:")
    assert "store busy" in second
    assert calls == ["store"]
    assert bot._sites["reused"]["title"] == "Old"


def test_extra_files_land_next_to_index(bot, tmp_path):
    tool = CreateSiteTool(bot)
    out = run(
        tool.execute(
            _msg(),
            name="multi",
            title="Multi",
            body='<!DOCTYPE html><html><head><link rel="stylesheet" href="style.css">'
            "</head><body></body></html>",
            files=json.dumps(
                {
                    "style.css": "body{background:#101014}",
                    "app.js": "console.log(1)",
                    "about/index.html": "<p>about</p>",
                }
            ),
        )
    )
    assert out.startswith("Site created:")
    root = tmp_path / "public" / "bot" / "multi"
    assert (root / "style.css").read_text() == "body{background:#101014}"
    assert (root / "app.js").read_text() == "console.log(1)"
    assert (root / "about" / "index.html").read_text() == "<p>about</p>"
    assert "style.css" in out
    assert "site_test" not in out


def test_files_accepts_a_list_of_objects(bot, tmp_path):
    tool = CreateSiteTool(bot)
    out = run(
        tool.execute(
            _msg(),
            name="listy",
            title="Listy",
            body=PAGE,
            files=[{"path": "data.json", "content": '{"ok":true}'}],
        )
    )
    assert out.startswith("Site created:")
    assert (tmp_path / "public" / "bot" / "listy" / "data.json").read_text() == '{"ok":true}'


@pytest.mark.parametrize(
    "bad", ["../../etc/passwd", ".hidden", "a/../../b", "shell.php", "x" * 90]
)
def test_file_paths_cannot_escape_or_execute(bot, bad):
    tool = CreateSiteTool(bot)
    out = run(
        tool.execute(_msg(), name="nope", title="Nope", body=PAGE, files={bad: "x"})
    )
    assert out.startswith("Error:")
    assert "unsafe" in out or "supported" in out


def test_absolute_paths_are_read_as_site_relative(bot, tmp_path):
    """Models write href="/style.css" meaning the site root, not the disk root."""
    out = run(
        CreateSiteTool(bot).execute(
            _msg(), name="rooted", title="Rooted", body=PAGE, files={"/style.css": "a{}"}
        )
    )
    assert out.startswith("Site created:")
    assert (tmp_path / "public" / "bot" / "rooted" / "style.css").read_text() == "a{}"
    assert not Path("/style.css").exists()


def test_body_can_come_from_files_index(bot, tmp_path):
    tool = CreateSiteTool(bot)
    out = run(
        tool.execute(
            _msg(), name="fromfiles", title="From files", files={"index.html": PAGE}
        )
    )
    assert out.startswith("Site created:")
    assert (tmp_path / "public" / "bot" / "fromfiles" / "index.html").read_text() == PAGE


def test_missing_everything_names_what_is_missing(bot):
    out = run(CreateSiteTool(bot).execute(_msg(), name="x"))
    assert out.startswith("Error: missing required params")
    assert "title" in out and "body" in out


def test_edit_read_write_replace_and_list(bot, tmp_path):
    run(CreateSiteTool(bot).execute(_msg(), name="edits", title="Edits", body=PAGE))
    edit = EditSiteTool(bot)

    listing = run(edit.execute(_msg(), name="edits", action="list"))
    assert "index.html" in listing

    read = run(edit.execute(_msg(), name="edits", action="read"))
    assert "<h1>hi</h1>" in read

    patched = run(
        edit.execute(
            _msg(), name="edits", action="replace", find="<h1>hi</h1>", replace="<h1>yo</h1>"
        )
    )
    assert patched.startswith("Patched index.html")
    page = (tmp_path / "public" / "bot" / "edits" / "index.html").read_text()
    assert "<h1>yo</h1>" in page and "<h1>hi</h1>" not in page

    wrote = run(
        edit.execute(_msg(), name="edits", action="write", path="s.css", content="a{}")
    )
    assert wrote.startswith("Wrote s.css")
    assert (tmp_path / "public" / "bot" / "edits" / "s.css").read_text() == "a{}"

    multi = run(
        edit.execute(
            _msg(),
            name="edits",
            action="write",
            files={"a.js": "1", "b.js": "2"},
        )
    )
    assert "a.js" in multi and "b.js" in multi
    assert (tmp_path / "public" / "bot" / "edits" / "a.js").read_text() == "1"

    twice = run(
        edit.execute(
            _msg(),
            name="edits",
            action="replace",
            path="a.js",
            find="1",
            replace="one",
            all=True,
        )
    )
    assert "1 occurrence" in twice or "occurrence" in twice
    assert (tmp_path / "public" / "bot" / "edits" / "a.js").read_text() == "one"


def test_replace_that_does_not_match_says_so(bot):
    run(CreateSiteTool(bot).execute(_msg(), name="nm", title="NM", body=PAGE))
    out = run(
        EditSiteTool(bot).execute(
            _msg(), name="nm", action="replace", find="not in there", replace="x"
        )
    )
    assert out.startswith("Error:")
    assert "byte-for-byte" in out


def test_edit_can_read_a_site_you_did_not_create(bot, tmp_path):
    run(CreateSiteTool(bot).execute(_msg(uid=1), name="mine", title="Mine", body=PAGE))
    out = run(EditSiteTool(bot).execute(_msg(uid=2), name="mine", action="read"))
    assert "<h1>hi</h1>" in out
    assert "belongs to someone else" not in out


def test_edit_cannot_change_another_users_site(bot):
    run(CreateSiteTool(bot).execute(_msg(uid=1), name="mine", title="Mine", body=PAGE))
    out = run(
        EditSiteTool(bot).execute(
            _msg(uid=2), name="mine", action="write", content="<p>nope</p>"
        )
    )
    assert "belongs to someone else" in out
    taken = run(
        CreateSiteTool(bot).execute(_msg(uid=2), name="mine", title="Mine", body=PAGE)
    )
    assert "already owned" in taken


def test_edit_cannot_read_outside_the_site(bot, tmp_path):
    run(CreateSiteTool(bot).execute(_msg(), name="jail", title="Jail", body=PAGE))
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    out = run(
        EditSiteTool(bot).execute(
            _msg(), name="jail", action="read", path="../../secret.txt"
        )
    )
    assert out.startswith("Error: bad path")


def test_delete_site_removes_files_metadata_and_store(bot, tmp_path):
    run(
        CreateSiteTool(bot).execute(
            _msg(), name="gone", title="Gone", body=PAGE, backend=True
        )
    )
    site_backend.kv_set(bot.config.DATA_DIR, "gone", "hits", 3)
    out = run(DeleteSiteTool(bot).execute(_msg(), name="gone"))
    assert out.startswith("Deleted site 'gone'")
    assert not (tmp_path / "public" / "bot" / "gone").exists()
    assert "gone" not in bot._sites
    assert site_backend.snapshot(bot.config.DATA_DIR, "gone") == {
        "kv": {},
        "collections": {},
    }


def test_delete_site_refuses_another_users_site(bot, tmp_path):
    run(CreateSiteTool(bot).execute(_msg(uid=1), name="mine", title="Mine", body=PAGE))
    out = run(DeleteSiteTool(bot).execute(_msg(uid=2), name="mine"))
    assert out.startswith("Error:")
    assert "belongs to someone else" in out
    assert "mine" in bot._sites
    assert (tmp_path / "public" / "bot" / "mine").exists()


def test_backend_flag_records_and_documents_itself(bot):
    out = run(
        CreateSiteTool(bot).execute(
            _msg(), name="guest", title="Guestbook", body=PAGE, backend=True
        )
    )
    assert "/api/site/guest" in out
    assert "items/NAME" in out
    assert bot._sites["guest"]["backend"] is True


def test_backend_can_be_toggled_and_inspected_after_the_fact(bot):
    run(CreateSiteTool(bot).execute(_msg(), name="later", title="Later", body=PAGE))
    edit = EditSiteTool(bot)
    assert "has no backend" in run(
        edit.execute(_msg(), name="later", action="backend")
    )
    on = run(edit.execute(_msg(), name="later", action="backend", backend="true"))
    assert "/api/site/later" in on
    assert bot._sites["later"]["backend"] is True

    site_backend.items_add(bot.config.DATA_DIR, "later", "notes", {"t": "hi"})
    status = run(edit.execute(_msg(), name="later", action="backend"))
    assert "notes[1]" in status
    assert "Cleared" in run(
        edit.execute(_msg(), name="later", action="backend", backend="clear")
    )
    assert run(edit.execute(_msg(), name="later", action="backend")).endswith("empty")


def test_permanent_and_extend_control_lifetime(bot):
    run(
        CreateSiteTool(bot).execute(
            _msg(), name="forever", title="Forever", body=PAGE, permanent=True
        )
    )
    assert bot._sites["forever"]["permanent"] is True
    listing = run(ListSitesTool(bot).execute(_msg()))
    assert "permanent" in listing

    run(CreateSiteTool(bot).execute(_msg(), name="temp", title="Temp", body=PAGE))
    out = run(EditSiteTool(bot).execute(_msg(), name="temp", action="extend"))
    assert "23h" in out or "24h" in out


def test_site_ttl_hours_is_not_hardcoded(bot):
    bot._control["site_ttl_hours"] = 1
    bot.control["site_ttl_hours"] = 1
    run(CreateSiteTool(bot).execute(_msg(), name="short", title="Short", body=PAGE))
    assert "0h 59m left" in run(ListSitesTool(bot).execute(_msg()))
    bot._control["site_ttl_hours"] = 0
    bot.control["site_ttl_hours"] = 0
    assert "permanent" in run(ListSitesTool(bot).execute(_msg()))


def test_rename_only_changes_the_title(bot):
    run(CreateSiteTool(bot).execute(_msg(), name="titled", title="Old", body=PAGE))
    out = run(
        EditSiteTool(bot).execute(_msg(), name="titled", action="rename", title="New")
    )
    assert "'New'" in out
    assert bot._sites["titled"]["title"] == "New"


def test_unknown_site_points_at_list_sites(bot):
    out = run(EditSiteTool(bot).execute(_msg(), name="ghost", action="list"))
    assert "no site named 'ghost'" in out
    assert "list_sites" in out


def test_refuses_to_publish_a_history_placeholder_as_the_page(bot, tmp_path):
    marker = "[large content omitted, 63199 chars]"
    out = run(
        CreateSiteTool(bot).execute(
            _msg(), name="oops", title="Oops", body=marker
        )
    )
    assert out.startswith("Error:")
    assert "placeholder" in out.lower()
    assert not (tmp_path / "public" / "bot" / "oops" / "index.html").exists()

    run(CreateSiteTool(bot).execute(_msg(), name="ok", title="Ok", body=PAGE))
    wrote = run(
        EditSiteTool(bot).execute(
            _msg(), name="ok", action="write", content=marker
        )
    )
    assert wrote.startswith("Error:")
    page = (tmp_path / "public" / "bot" / "ok" / "index.html").read_text()
    assert PAGE in page
    assert marker not in page


def test_edit_site_read_returns_complete_page_over_60k(bot, tmp_path):
    huge = "<!DOCTYPE html><html><body>" + ("x" * 63_199) + "</body></html>"
    msg = _msg()
    run(CreateSiteTool(bot).execute(msg, name="big", title="Big", body=huge))
    out = run(EditSiteTool(bot).execute(msg, name="big", action="read"))
    assert "too big to return" not in out
    assert str(len(huge)) in out
    assert huge in out
    assert "omitted" not in out


def test_edit_site_refuses_a_second_identical_read(bot):
    msg = _msg()
    run(CreateSiteTool(bot).execute(msg, name="once", title="Once", body=PAGE))
    edit = EditSiteTool(bot)
    first = run(edit.execute(msg, name="once", action="read"))
    assert "<h1>hi</h1>" in first
    second = run(edit.execute(msg, name="once", action="read"))
    assert "Already returned" in second
    assert "<h1>hi</h1>" not in second


def test_edit_site_stops_a_read_only_loop(bot):
    from bot_tools import SITE_IDLE_READ_LIMIT, SITE_READ_LOOP_MARKER

    msg = _msg()
    run(CreateSiteTool(bot).execute(msg, name="loop", title="Loop", body=PAGE))
    edit = EditSiteTool(bot)
    for i in range(SITE_IDLE_READ_LIMIT):
        run(edit.execute(msg, name="loop", action="read", start_line=i + 1))
    last = run(edit.execute(msg, name="loop", action="read", start_line=99))
    assert SITE_READ_LOOP_MARKER in last


def test_edit_write_resets_the_read_loop(bot):
    from bot_tools import SITE_IDLE_READ_LIMIT, SITE_READ_LOOP_MARKER

    msg = _msg()
    run(CreateSiteTool(bot).execute(msg, name="reset", title="Reset", body=PAGE))
    edit = EditSiteTool(bot)
    for i in range(SITE_IDLE_READ_LIMIT):
        run(edit.execute(msg, name="reset", action="read", start_line=i + 1))
    run(edit.execute(msg, name="reset", action="write", content=PAGE))
    out = run(edit.execute(msg, name="reset", action="read"))
    assert "<h1>hi</h1>" in out
    assert SITE_READ_LOOP_MARKER not in out


def test_site_guards_work_on_slotted_discord_messages(bot):
    """discord.py Message has __slots__; stashing _site_idle_reads on it raises."""
    from bot_tools import (
        SITE_READ_LOOP_MARKER,
        site_read_loop_guard,
        site_test_repeat_guard,
    )

    class SlottedMessage:
        __slots__ = ("author",)

        def __init__(self):
            self.author = SimpleNamespace(id=42, display_name="tester")

    run(CreateSiteTool(bot).execute(_msg(), name="slot", title="Slot", body=PAGE))
    msg = SlottedMessage()
    assert site_read_loop_guard(msg, key="a", label="a", action="read") is None
    assert site_read_loop_guard(msg, key="a", label="a", action="read") is not None
    assert site_read_loop_guard(msg, key="a", label="a", action="write") is None
    assert site_test_repeat_guard(msg, "https://example/test") is None
    assert site_test_repeat_guard(msg, "https://example/test") is None
    third = site_test_repeat_guard(msg, "https://example/test")
    assert third is not None
    assert SITE_READ_LOOP_MARKER in third
    out = run(EditSiteTool(bot).execute(msg, name="slot", action="read"))
    assert "_site_idle_reads" not in out
    assert "<h1>hi</h1>" in out


def test_site_read_loop_guard_ignores_recycled_object_ids():
    from bot_tools import _SITE_TURN_STATE, site_read_loop_guard

    class Holder:
        pass

    msg = Holder()
    _SITE_TURN_STATE[id(msg)] = {
        "idle": 99,
        "test_counts": {},
        "read_cache": {"a"},
        "_obj": object(),
    }
    assert site_read_loop_guard(msg, key="a", label="a", action="read") is None


def test_api_path_warnings_flag_absolute_fetch():
    from bot_tools import _site_api_path_warnings

    body = '<script>fetch("/api/notes").then(r=>r.json())</script>'
    warns = _site_api_path_warnings("quantum-forge", body, [])
    assert any("index.html" in w and "RELATIVE" in w.upper() or "relative" in w for w in warns), warns


def test_api_path_warnings_flag_wrong_slug_and_hardcoded_ws():
    from bot_tools import _site_api_path_warnings

    files = [
        {"path": "app.js", "bytes": b'fetch("/bot/other-site/api/notes"); new WebSocket("ws://x/bot/q/api/ws")'},
    ]
    warns = _site_api_path_warnings("quantum-forge", None, files)
    assert any("other-site" in w for w in warns), warns
    assert any("WebSocket" in w for w in warns), warns


def test_api_path_warnings_quiet_on_relative_paths():
    from bot_tools import _site_api_path_warnings

    body = '<script>fetch("api/notes")</script>'
    assert _site_api_path_warnings("quantum-forge", body, []) == []


def test_unowned_slug_does_not_inherit_the_old_store(bot):
    path = site_backend.store_path(bot.config.DATA_DIR, "guest")
    path.parent.mkdir(parents=True)
    path.write_text('{"secret": "old-tenant"}', encoding="utf-8")
    bot._sites["guest"] = {"title": "Old", "user_id": ""}
    out = run(
        CreateSiteTool(bot).execute(_msg(uid=7), name="guest", title="New", body=PAGE)
    )
    assert not out.startswith("Error:")
    assert not path.exists()
