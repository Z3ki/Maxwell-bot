import asyncio
import io
import tarfile
from types import SimpleNamespace

import pytest

from plugins.maxwell_extras.task_jobs import _files_note, reported_files
from plugins.user_storage.store import (
    MB,
    StorageError,
    UserStorage,
    clean_path,
    load_or_create_key,
    tar_bytes,
    valid_handle,
)
from plugins.user_storage.tool import StorageTool
from plugins.user_storage.urls import public_url

PREMIUM = "111111111111111111"
FREE = "222222222222222222"


@pytest.fixture
def storage(tmp_path):
    plans = {PREMIUM: "premium"}
    return UserStorage(tmp_path / "st", plan_lookup=lambda uid: plans.get(uid, "free"))


def _tar(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, kind, data in entries:
            member = tarfile.TarInfo(name)
            if kind == "dir":
                member.type = tarfile.DIRTYPE
                tar.addfile(member)
            elif kind == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = data
                tar.addfile(member)
            else:
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
    buffer.seek(0)
    return buffer


@pytest.mark.parametrize("raw", ["../x", "a/../b", "a//b", "./a", "a/\x01b", ""])
def test_clean_path_refuses_escapes_and_junk(raw):
    with pytest.raises(StorageError):
        clean_path(raw)


def test_clean_path_strips_mount_prefix():
    assert clean_path("/storage/public/game/index.html") == "public/game/index.html"
    assert clean_path("storage/a.txt") == "a.txt"
    assert clean_path("/storage", allow_root=True) == ""


@pytest.mark.parametrize(
    "handle", ["admin", "api", "-x", "x-", "a--b", "A B", "x" * 31]
)
def test_bad_handles(handle):
    with pytest.raises(StorageError):
        valid_handle(handle)


def test_owner_handle_is_allowed():
    assert valid_handle("Z3ki") == "z3ki"


def test_free_users_get_nothing(storage):
    assert storage.quota(FREE) == 0
    with pytest.raises(StorageError, match="Premium"):
        storage.write(FREE, "a.txt", b"x")
    assert storage.quota(PREMIUM) == 100 * MB


def test_write_read_move_delete_round_trip(storage):
    storage.write(PREMIUM, "/storage/notes/a.txt", b"hello")
    assert storage.read(PREMIUM, "notes/a.txt") == b"hello"
    storage.move(PREMIUM, "notes/a.txt", "public/a.txt")
    assert storage.file_path(PREMIUM, "notes/a.txt") is None
    assert storage.read(PREMIUM, "public/a.txt") == b"hello"
    paths = [entry.path for entry in storage.list(PREMIUM)]
    assert paths == ["notes", "public", "public/a.txt"]
    storage.delete(PREMIUM, "public")
    assert storage.usage(PREMIUM) == (0, 0)


def test_quota_and_file_count(storage, monkeypatch):
    monkeypatch.setenv("MAXWELL_STORAGE_PREMIUM_MB", "1")
    storage.write(PREMIUM, "a.bin", b"x" * (MB - 10))
    with pytest.raises(StorageError, match="full"):
        storage.write(PREMIUM, "b.bin", b"x" * 100)
    # Overwriting counts the old size as freed.
    storage.write(PREMIUM, "a.bin", b"x" * (MB - 5))
    monkeypatch.setenv("MAXWELL_STORAGE_MAX_FILES", "10")
    for index in range(9):
        storage.write(PREMIUM, f"f{index}", b"")
    with pytest.raises(StorageError, match="at most 10 files"):
        storage.write(PREMIUM, "one-too-many", b"")


def test_path_through_a_file_is_not_an_os_error(storage):
    storage.write(PREMIUM, "notes.txt", b"hi")
    assert storage.file_path(PREMIUM, "notes.txt/x") is None
    with pytest.raises(StorageError, match="not found"):
        storage.read(PREMIUM, "notes.txt/x")
    with pytest.raises(StorageError, match="does not exist"):
        storage.list(PREMIUM, "notes.txt/x")
    with pytest.raises(StorageError, match="nothing at that path"):
        storage.delete(PREMIUM, "notes.txt/x")
    with pytest.raises(StorageError, match="file, not a folder"):
        storage.write(PREMIUM, "notes.txt/x", b"a")
    assert storage.read(PREMIUM, "notes.txt") == b"hi"


def test_host_symlinks_are_never_followed(storage, tmp_path):
    storage.write(PREMIUM, "real.txt", b"ok")
    secret = tmp_path / "secret"
    secret.write_text("nope")
    (storage.user_dir(PREMIUM) / "evil").symlink_to(secret)
    assert storage.file_path(PREMIUM, "evil") is None
    with pytest.raises(StorageError):
        storage.read(PREMIUM, "evil")
    assert [entry.path for entry in storage.list(PREMIUM)] == ["real.txt"]


def test_import_merges_changes_and_drops_hostile_members(storage):
    storage.write(PREMIUM, "a/b.txt", b"old")
    storage.write(PREMIUM, "public/index.html", b"<h1>hi</h1>")
    spool, base, generation = storage.export_tar(PREMIUM)
    spool.close()
    archive = _tar(
        [
            ("./a", "dir", b""),
            ("./a/b.txt", "file", b"new"),
            ("./c.bin", "file", b"\x00\x01"),
            ("./public", "dir", b""),
            ("./evil", "link", "/etc/passwd"),
            ("../escape", "file", b"x"),
        ]
    )
    result = storage.import_tar(PREMIUM, archive, base, base_generation=generation)
    assert result.clean and not result.error
    assert result.written == 2 and result.deleted == 1
    assert "evil" in result.skipped and any("escape" in item for item in result.skipped)
    assert storage.read(PREMIUM, "a/b.txt") == b"new"
    assert storage.file_path(PREMIUM, "public/index.html") is None
    assert storage.file_path(PREMIUM, "evil") is None
    assert result.manifest == storage.manifest(PREMIUM)


def test_import_keeps_host_edits_made_meanwhile(storage):
    storage.write(PREMIUM, "shared.txt", b"v1")
    spool, base, generation = storage.export_tar(PREMIUM)
    spool.close()
    storage.write(PREMIUM, "from-dashboard.txt", b"host")
    archive = io.BytesIO(tar_bytes({"shared.txt": b"v1", "from-shell.txt": b"guest"}))
    result = storage.import_tar(PREMIUM, archive, base, base_generation=generation)
    assert not result.clean
    assert storage.read(PREMIUM, "from-dashboard.txt") == b"host"
    assert storage.read(PREMIUM, "from-shell.txt") == b"guest"


def test_import_refuses_over_quota(storage, monkeypatch):
    monkeypatch.setenv("MAXWELL_STORAGE_PREMIUM_MB", "1")
    spool, base, generation = storage.export_tar(PREMIUM)
    spool.close()
    archive = io.BytesIO(tar_bytes({"big": b"x" * (2 * MB)}))
    result = storage.import_tar(PREMIUM, archive, base, base_generation=generation)
    assert "quota" in result.error
    assert storage.usage(PREMIUM) == (0, 0)


def test_handles_and_signed_links(storage):
    assert storage.ensure_handle(PREMIUM, "Cool Person!") == "cool-person"
    assert storage.ensure_handle(FREE, "cool person") == "cool-person-2"
    assert storage.uid_for_handle("COOL-person") == PREMIUM
    with pytest.raises(StorageError, match="taken"):
        storage.set_handle(FREE, "cool-person")
    signature, expires = storage.sign(PREMIUM, "a.txt", ttl=3600)
    assert storage.verify(PREMIUM, "/storage/a.txt", str(expires), signature)
    assert not storage.verify(PREMIUM, "b.txt", str(expires), signature)
    assert not storage.verify(FREE, "a.txt", str(expires), signature)
    assert not storage.verify(PREMIUM, "a.txt", str(expires + 1), signature)
    assert not storage.verify(PREMIUM, "a.txt", "1", signature)


def test_wipe_removes_files_and_handle(storage):
    storage.write(PREMIUM, "public/index.html", b"x")
    storage.set_handle(PREMIUM, "gone")
    storage.wipe(PREMIUM)
    assert storage.usage(PREMIUM) == (0, 0)
    assert storage.uid_for_handle("gone") is None
    assert storage.generation(PREMIUM) == 0


def test_public_urls(monkeypatch):
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://bot.example/dev/")
    assert public_url("cat", "public/index.html") == "https://bot.example/dev/u/cat/"
    assert (
        public_url("cat", "public/cat 3d/index.html")
        == "https://bot.example/dev/u/cat/cat%203d/"
    )
    assert public_url("cat", "notes.txt") == ""


def _message(uid=PREMIUM, name="tester"):
    return SimpleNamespace(author=SimpleNamespace(id=int(uid), name=name))


def test_storage_tool(storage, monkeypatch):
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://bot.example")
    tool = StorageTool(SimpleNamespace(_user_storage=storage))

    def run(**kwargs):
        return asyncio.run(tool.execute(_message(), **kwargs))

    saved = run(action="write", path="public/cat-3d/index.html", content="<p>cat</p>")
    assert saved.endswith("live at https://bot.example/u/tester/cat-3d/")
    assert (
        run(action="link", path="public/cat-3d")
        == "Public URL: https://bot.example/u/tester/cat-3d/"
    )
    run(action="write", path="raw.bin", content="AAEC", encoding="base64")
    assert "binary file" in run(action="read", path="raw.bin")
    private = run(action="link", path="raw.bin", hours=2)
    assert private.startswith("Private download link") and "/u/_s/" + PREMIUM in private
    assert run(action="link", path="missing.txt") == "Error: no file at that path"
    assert run(action="read", path="../etc/passwd").startswith("Error:")
    assert run(action="set_handle", handle="admin") == "Error: that handle is reserved"
    assert '"handle": "tester"' in run(action="status")
    refused = asyncio.run(tool.execute(_message(FREE), action="status"))
    assert refused.startswith("Error:") and "do not mention prices" in refused


def test_reported_files_take_any_type_but_not_folders():
    text = (
        "Built /workspace/out/pack.mcaddon and /workspace/out/ plus /storage/jobs/1/site.zip. "
        "Also /workspace/../etc/passwd.txt, /workspace/README, /workspace/a.txt, /workspace/b.txt"
    )
    assert reported_files(text) == [
        "/workspace/out/pack.mcaddon",
        "/storage/jobs/1/site.zip",
        "/workspace/a.txt",
        "/workspace/b.txt",
    ]


def test_files_note_mentions_kept_and_unsent():
    note = _files_note(
        {
            "kept": ["/storage/jobs/abc/pack.mcaddon"],
            "unsent": ["/storage/jobs/abc/pack.mcaddon", "/workspace/huge.bin"],
        }
    )
    assert "Saved to `/storage/jobs/abc/`" in note
    assert "ask me for a download link" in note
    assert "Couldn't attach `/workspace/huge.bin`." in note
    assert _files_note({}) == ""


def test_privacy_deletion_wipes_storage_and_schedules(tmp_path):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore
    from plugins.schedules.store import ScheduleStore
    from privacy_data import delete_data

    storage = UserStorage(
        tmp_path / "plugins/user_storage", plan_lookup=lambda uid: "premium"
    )
    storage.write(PREMIUM, "public/index.html", b"x")
    storage.set_handle(PREMIUM, "mine")
    schedules = ScheduleStore(tmp_path / "plugins/schedules/schedules.sqlite3")
    schedules.add(
        user_id=PREMIUM,
        name="n",
        kind="cron",
        spec="@daily",
        tz="UTC",
        prompt="p",
        channel_id="",
        guild_id="",
        dm=True,
        next_run=0,
    )
    prefs = UserPreferenceStore(
        tmp_path / "plugins/maxwell_extras/user_preferences.json"
    )
    result = delete_data(tmp_path, PREMIUM, prefs)
    assert (
        "persistent_storage" not in result["failed"]
        and "schedules" not in result["failed"]
    )
    assert storage.usage(PREMIUM) == (0, 0)
    assert storage.uid_for_handle("mine") is None
    assert schedules.list_user(PREMIUM) == []


def test_zero_byte_key_file_is_repaired(tmp_path):
    key_path = tmp_path / "link.key"
    key_path.write_bytes(b"")
    key = load_or_create_key(key_path)
    assert len(key) == 32
    assert key_path.read_bytes() == key
