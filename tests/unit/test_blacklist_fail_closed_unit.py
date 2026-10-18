"""Regression: a corrupt blacklist.json must not unblock everyone."""

from __future__ import annotations

import json
from types import SimpleNamespace

from bot import MaxwellBot


def _bot(tmp_path):
    bot = object.__new__(MaxwellBot)
    bot.config = SimpleNamespace(DATA_DIR=str(tmp_path))
    bot._blacklist = set()
    return bot


def test_corrupt_blacklist_keeps_previously_loaded_entries(tmp_path):
    path = tmp_path / "blacklist.json"
    path.write_text(json.dumps(["42", "43"]), encoding="utf-8")
    bot = _bot(tmp_path)
    bot._load_blacklist(quiet=True)
    assert bot._blacklist == {"42", "43"}

    path.write_text("{truncated", encoding="utf-8")
    bot._load_blacklist(quiet=True)
    assert bot._blacklist == {"42", "43"}


def test_fixed_blacklist_file_is_picked_up_again(tmp_path):
    path = tmp_path / "blacklist.json"
    path.write_text("{truncated", encoding="utf-8")
    bot = _bot(tmp_path)
    bot._blacklist = {"42"}
    bot._load_blacklist(quiet=True)
    assert bot._blacklist == {"42"}
    path.write_text(json.dumps(["7"]), encoding="utf-8")
    bot._load_blacklist(quiet=True)
    assert bot._blacklist == {"7"}


def test_first_load_of_a_corrupt_file_starts_empty(tmp_path):
    (tmp_path / "blacklist.json").write_text("{bad", encoding="utf-8")
    bot = _bot(tmp_path)
    bot._load_blacklist(quiet=True)
    assert bot._blacklist == set()
