"""Persisted settings: admins, per-server toggles, sites and DM gating."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import discord
import pytest

from bot import MaxwellBot

OWNER = "111"
CREATOR = "222"


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    for name in ("MAXWELL_OWNER_IDS", "CREATOR_ID", "CREATOR_NAME"):
        monkeypatch.delenv(name, raising=False)


def _bot(tmp_path, **config):
    bot = object.__new__(MaxwellBot)
    defaults = {
        "DATA_DIR": str(tmp_path),
        "MAXWELL_OWNER_IDS": [OWNER],
        "CREATOR_ID": "",
        "PROGRESS_MESSAGES": True,
    }
    defaults.update(config)
    bot.config = SimpleNamespace(**defaults)
    bot._admins = set()
    bot._auto_channels = set()
    bot._progress_servers = set()
    bot._progress_servers_off = set()
    bot._ticket_greeting_servers = set()
    bot._shell_whitelist = set()
    bot._blacklist = set()
    bot._sites = {}
    bot._sites_mtime = None
    bot._control = {}
    return bot


def _write(tmp_path, name, data):
    (tmp_path / name).write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )


# -- admins -----------------------------------------------------------------


def test_admins_are_configured_owners_plus_creator(tmp_path):
    bot = _bot(tmp_path, CREATOR_ID=CREATOR)
    bot._load_admins(quiet=True)
    assert bot._admins == {OWNER, CREATOR}
    assert bot._is_admin(int(OWNER)) and bot._is_admin(CREATOR)
    assert not bot._is_admin("999")


@pytest.mark.parametrize(
    "payload",
    [["333", 444], {"admins": ["333"], "owners": [444]}, {"user_ids": ["333", "444"]}],
)
def test_admins_json_list_and_object_shapes(tmp_path, payload):
    _write(tmp_path, "admins.json", payload)
    bot = _bot(tmp_path)
    bot._load_admins(quiet=True)
    assert {"333"} <= bot._admins and OWNER in bot._admins


def test_admins_json_ignores_non_list_values(tmp_path):
    _write(tmp_path, "admins.json", {"admins": "333", "owners": {"x": 1}})
    bot = _bot(tmp_path)
    bot._load_admins(quiet=True)
    assert bot._admins == {OWNER}


def test_corrupt_admins_json_falls_back_to_configured_owners_only(tmp_path):
    _write(tmp_path, "admins.json", "{broken")
    bot = _bot(tmp_path)
    bot._admins = {"stale-extra"}
    bot._load_admins(quiet=True)
    assert bot._admins == {OWNER}


def test_no_owner_configured_means_no_admins(tmp_path):
    bot = _bot(tmp_path, MAXWELL_OWNER_IDS=[])
    bot._load_admins(quiet=True)
    assert bot._admins == set()
    assert not bot._is_admin("")


def test_save_admins_writes_sorted_list(tmp_path):
    bot = _bot(tmp_path)
    bot._admins = {"3", "1", "2"}
    bot._save_admins()
    assert json.loads((tmp_path / "admins.json").read_text()) == ["1", "2", "3"]


# -- per-server toggles -------------------------------------------------------


def test_progress_defaults_follow_config_with_server_overrides(tmp_path):
    bot = _bot(tmp_path, PROGRESS_MESSAGES=False)
    assert bot._progress_enabled("10") is False
    bot._progress_servers = {"10"}
    bot._progress_servers_off = {"20"}
    assert bot._progress_enabled("10") is True
    bot.config.PROGRESS_MESSAGES = True
    assert bot._progress_enabled("20") is False
    assert bot._progress_enabled("30") is True
    assert bot._progress_enabled("DM") is False
    assert bot._progress_enabled("") is False


def test_progress_servers_round_trip(tmp_path):
    bot = _bot(tmp_path)
    bot._progress_servers = {"2", "1"}
    bot._progress_servers_off = {"9"}
    bot._save_progress_servers()
    other = _bot(tmp_path)
    other._load_progress_servers(quiet=True)
    assert other._progress_servers == {"1", "2"}
    assert other._progress_servers_off == {"9"}


def test_ticket_greeting_is_opt_in_per_server(tmp_path):
    _write(tmp_path, "ticket_greeting_servers.json", ["5"])
    bot = _bot(tmp_path)
    bot._load_ticket_greeting_servers(quiet=True)
    assert bot._ticket_greeting_enabled("5")
    assert not bot._ticket_greeting_enabled("6")
    assert not bot._ticket_greeting_enabled("DM")
    assert not bot._ticket_greeting_enabled("")


def test_str_set_files_ignore_non_list_payloads(tmp_path):
    _write(tmp_path, "auto_channels.json", {"not": "a list"})
    bot = _bot(tmp_path)
    bot._auto_channels = {"keep"}
    bot._load_auto_channels(quiet=True)
    assert bot._auto_channels == {"keep"}


def test_auto_channels_round_trip_stringifies_ids(tmp_path):
    _write(tmp_path, "auto_channels.json", [1, "2"])
    bot = _bot(tmp_path)
    bot._load_auto_channels(quiet=True)
    assert bot._auto_channels == {"1", "2"}


def test_shell_whitelist_accepts_any_iterable_json(tmp_path):
    _write(tmp_path, "shell_whitelist.json", {"7": True})
    bot = _bot(tmp_path)
    bot._load_shell_whitelist(quiet=True)
    assert bot._shell_whitelist == {"7"}


def test_blacklist_round_trip(tmp_path):
    bot = _bot(tmp_path)
    bot._blacklist = {"42"}
    bot._save_blacklist()
    fresh = _bot(tmp_path)
    fresh._load_blacklist(quiet=True)
    assert fresh._blacklist == {"42"}


def test_save_failure_is_logged_not_raised(tmp_path, caplog):
    bot = _bot(tmp_path / "missing-dir" / "deeper")
    bot._blacklist = {"1"}
    blocker = tmp_path / "missing-dir"
    blocker.write_text("a file where a directory should be", encoding="utf-8")
    bot._save_blacklist()
    assert any("Failed to save blacklist" in r.getMessage() for r in caplog.records)


# -- sites --------------------------------------------------------------------


def test_load_sites_filters_entries_and_caches_by_mtime(tmp_path):
    _write(tmp_path, "sites.json", {"good": {"created_at": 1}, "bad": "x"})
    bot = _bot(tmp_path)
    bot._load_sites(quiet=True)
    assert set(bot._sites) == {"good"}
    path = tmp_path / "sites.json"
    stamp = path.stat().st_mtime
    path.write_text(json.dumps({"new": {}}), encoding="utf-8")
    os.utime(path, (stamp, stamp))
    bot._load_sites(quiet=True)
    assert set(bot._sites) == {"good"}


def test_corrupt_sites_file_keeps_previous_map(tmp_path):
    bot = _bot(tmp_path)
    bot._sites = {"existing": {"created_at": 1}}
    _write(tmp_path, "sites.json", "{broken")
    bot._load_sites(quiet=True)
    assert bot._sites == {"existing": {"created_at": 1}}


def test_non_object_sites_file_loads_as_empty(tmp_path):
    _write(tmp_path, "sites.json", [1, 2])
    bot = _bot(tmp_path)
    bot._sites = {"old": {}}
    bot._load_sites(quiet=True)
    assert bot._sites == {}


@pytest.mark.parametrize(
    "entry, control_ttl, age_hours, expired",
    [
        ({"permanent": True}, 1, 1000, False),
        ({"ttl_hours": 2}, 0, 3, True),
        ({"ttl_hours": 2}, 0, 1, False),
        ({"ttl_hours": 0}, 1, 1000, False),
        ({}, 1, 2, True),
        ({}, 0, 10_000, False),
        ({"ttl_hours": "junk"}, 0, 23, False),
        ({"ttl_hours": "junk"}, 0, 25, True),
    ],
)
def test_site_expiry_rules(tmp_path, entry, control_ttl, age_hours, expired):
    bot = _bot(tmp_path)
    bot._control = {"site_ttl_hours": control_ttl}
    now = 1_000_000.0
    record = {"created_at": now - age_hours * 3600, **entry}
    assert bot._site_expired(record, now) is expired


# -- DM gating ------------------------------------------------------------------


def _dm_message():
    channel = object.__new__(discord.DMChannel)
    return SimpleNamespace(channel=channel)


def test_guild_messages_are_never_dm_gated(tmp_path):
    bot = _bot(tmp_path)
    bot._control = {"reply_dms": False}
    assert bot._dm_replies_allowed(SimpleNamespace(channel=SimpleNamespace())) is True


def test_dm_replies_follow_reply_dms(tmp_path):
    bot = _bot(tmp_path)
    bot._control = {"reply_dms": True}
    assert bot._dm_replies_allowed(_dm_message()) is True
    bot._control = {"reply_dms": False}
    assert bot._dm_replies_allowed(_dm_message()) is False
    bot._control = {}
    assert bot._dm_replies_allowed(_dm_message()) is False
