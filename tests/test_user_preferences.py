"""Preference persistence and hot-read regressions."""
from pathlib import Path

import pytest

from plugins.maxwell_extras import user_preferences as mod


def test_context_default_is_50_but_saved_choices_are_preserved(tmp_path):
    store = mod.UserPreferenceStore(tmp_path / "prefs.json")
    assert store.get("1")["defaults"]["context"] == 50
    store.set_default("1", "context", 25)
    assert store.get("1")["defaults"]["context"] == 25
    assert store.get("2")["defaults"]["context"] == 50


def test_unchanged_reads_reuse_cache_without_exposing_mutable_state(tmp_path, monkeypatch):
    store = mod.UserPreferenceStore(tmp_path / "prefs.json")
    store.set_default("1", "mode", "research")
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    for _ in range(20):
        row = store.get("1")
        assert row["defaults"]["mode"] == "research"
        row["defaults"]["mode"] = "code"
    assert len(reads) == 1


def test_cache_detects_other_instance_atomic_writes_and_deletion(tmp_path):
    path = tmp_path / "prefs.json"
    first = mod.UserPreferenceStore(path)
    second = mod.UserPreferenceStore(path)
    first.set_default("1", "visibility", "private")
    assert second.get("1")["defaults"]["visibility"] == "private"
    first.reset_default("1", "visibility")
    assert second.get("1")["defaults"]["visibility"] == "public"
    first.set_default("1", "visibility", "private")
    assert second.get("1")["defaults"]["visibility"] == "private"
    path.unlink()
    assert second.get("1")["defaults"]["visibility"] == "public"


def test_failed_write_does_not_change_cached_or_persisted_choice(tmp_path, monkeypatch):
    store = mod.UserPreferenceStore(tmp_path / "prefs.json")
    store.set_default("1", "visibility", "private")
    store.get("1")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_atomic_json_write_sync", fail)
    with pytest.raises(OSError):
        store.set_default("1", "visibility", "public")
    assert store.get("1")["defaults"]["visibility"] == "private"


@pytest.mark.parametrize("content", ['{"users":', '[]', '{"users": []}'])
def test_corrupt_preferences_are_not_overwritten(tmp_path, content):
    path = tmp_path / "prefs.json"
    path.write_text(content)
    store = mod.UserPreferenceStore(path)
    row = store.get("1")
    assert row["defaults"]["visibility"] == "private"
    assert row["defaults"]["context"] == 0
    with pytest.raises(ValueError, match="refusing to overwrite"):
        store.set_default("1", "mode", "code")
    assert path.read_text() == content


def test_content_permission_uses_one_locked_snapshot(tmp_path, monkeypatch):
    store = mod.UserPreferenceStore(tmp_path / "prefs.json")
    store.set_default("1", "tracking", "on")
    read = store._read
    snapshots = []

    def snapshot():
        root = read()
        snapshots.append(root)
        return root

    monkeypatch.setattr(store, "_read", snapshot)
    assert store.content_allowed("1") is True
    assert len(snapshots) == 1


def test_content_permission_rejects_revocation_despite_stale_public_preferences(
    tmp_path, monkeypatch
):
    store = mod.UserPreferenceStore(tmp_path / "prefs.json")
    store.set_default("1", "tracking", "on")
    stale = store.get("1")
    store.set_default("1", "tracking", "off")
    # Reproduce the stale opt-in result that could escape get() before a
    # concurrent revocation acquired the lock for content_allowed's next read.
    monkeypatch.setattr(store, "get", lambda _uid: stale)
    assert store.content_allowed("1") is False


def test_content_permission_preserves_deletion_barrier_and_fails_closed(tmp_path):
    store = mod.UserPreferenceStore(tmp_path / "prefs.json")
    store.delete_personal("1")
    deleted_at = store._read()["users"]["1"]["deleted_at"]
    store.set_default("1", "tracking", "on")
    assert store.content_allowed("1", started_at=deleted_at) is False
    assert store.content_allowed("1", started_at=deleted_at + 1) is True
    assert store.content_allowed("2") is False
    store.path.write_text('{"users":')
    assert store.content_allowed("1") is False
