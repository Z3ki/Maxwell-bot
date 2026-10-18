"""Backend data permissions: least-permissive modes the site uid can still use."""

from __future__ import annotations

import os
import sqlite3
import stat
from types import SimpleNamespace

import pytest

import site_server


def _info(gid):
    return SimpleNamespace(st_gid=gid)


@pytest.mark.parametrize(
    "gid, directory, expected",
    [
        (0, True, 0o1707),
        (0, False, 0o606),
        (site_server.SITE_GID, True, 0o1770),
        (site_server.SITE_GID, False, 0o660),
    ],
)
def test_site_access_mode(gid, directory, expected):
    assert site_server._site_access_mode(_info(gid), directory=directory) == expected


@pytest.mark.parametrize("gid", [0, 1234, site_server.SITE_GID])
@pytest.mark.parametrize("directory", [True, False])
def test_site_access_mode_never_grants_both_group_and_other(gid, directory):
    mode = site_server._site_access_mode(_info(gid), directory=directory)
    assert not (mode & 0o070 and mode & 0o007)
    assert mode & 0o600 == 0o600  # controller keeps owner read/write


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def _legacy(data_dir):
    path = site_server.state_dir(data_dir, "demo")
    path.mkdir(parents=True)
    (path / "nested").mkdir()
    database = path / "app.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('keep')")
    database.chmod(0o600)
    (path / "nested" / "blob.bin").write_bytes(b"x")
    return path, database


def _deny_chown(monkeypatch):
    def denied(*_a, **_k):
        raise PermissionError("no chown capability")

    monkeypatch.setattr(os, "fchown", denied)


def test_cap_dropped_controller_grants_other_access_only(data_dir, monkeypatch):
    monkeypatch.setattr(site_server, "SITE_GID", os.getgid() + 1)
    _deny_chown(monkeypatch)
    path, database = _legacy(data_dir)
    site_server.prepare_state_dir(data_dir, "demo")
    assert stat.S_IMODE(path.stat().st_mode) == 0o1707
    assert stat.S_IMODE((path / "nested").stat().st_mode) == 0o1707
    assert stat.S_IMODE(database.stat().st_mode) == 0o606
    assert stat.S_IMODE((path / "nested" / "blob.bin").stat().st_mode) == 0o606
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT v FROM t").fetchall() == [("keep",)]


def test_site_group_paths_get_group_access_and_no_other(data_dir, monkeypatch):
    monkeypatch.setattr(site_server, "SITE_GID", os.getgid())
    _deny_chown(monkeypatch)
    path, database = _legacy(data_dir)
    site_server.prepare_state_dir(data_dir, "demo")
    assert stat.S_IMODE(path.stat().st_mode) == 0o1770
    assert stat.S_IMODE(database.stat().st_mode) == 0o660


def test_previous_world_writable_modes_are_tightened_on_next_prepare(
    data_dir, monkeypatch
):
    monkeypatch.setattr(site_server, "SITE_GID", os.getgid() + 1)
    _deny_chown(monkeypatch)
    path, database = _legacy(data_dir)
    path.chmod(0o1777)
    database.chmod(0o666)
    site_server.prepare_state_dir(data_dir, "demo")
    assert stat.S_IMODE(path.stat().st_mode) == 0o1707
    assert stat.S_IMODE(database.stat().st_mode) == 0o606


def test_successful_chown_keeps_owner_group_mode(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))
    path, database = _legacy(data_dir)
    site_server.prepare_state_dir(data_dir, "demo")
    assert (site_server.SITE_UID, site_server.SITE_GID) in calls
    assert stat.S_IMODE(database.stat().st_mode) == 0o660
