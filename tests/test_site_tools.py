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
    control = {"create_site_quota_per_user": 50}
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


def test_replace_that_does_not_match_says_so(bot):
    run(CreateSiteTool(bot).execute(_msg(), name="nm", title="NM", body=PAGE))
    out = run(
        EditSiteTool(bot).execute(
            _msg(), name="nm", action="replace", find="not in there", replace="x"
        )
    )
    assert out.startswith("Error:")
    assert "byte-for-byte" in out


def test_edit_refuses_a_site_you_do_not_own(bot):
    run(CreateSiteTool(bot).execute(_msg(uid=1), name="mine", title="Mine", body=PAGE))
    out = run(EditSiteTool(bot).execute(_msg(uid=2), name="mine", action="read"))
    assert "belongs to someone else" in out


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
