"""Non-security hashes keep their exact output after usedforsecurity=False."""

from __future__ import annotations

import hashlib
import json

from api.state import _context_entry_id
from plugins.lastfm.client import signature


def test_context_entry_id_is_stable_short_sha1_of_normalized_entry():
    raw = {"scope": "global", "content": "  likes   tea  ", "created_at": "2026-01-01"}
    stable = json.dumps(
        {
            "scope": "global",
            "content": "likes tea",
            "created_at": "2026-01-01",
            "source_user_id": "",
            "source_channel_id": "",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    expected = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:8]
    assert _context_entry_id(raw) == expected
    assert _context_entry_id(dict(raw)) == expected
    assert _context_entry_id({**raw, "content": "other"}) != expected


def test_context_entry_id_keeps_an_existing_id():
    assert _context_entry_id({"id": "  abc123  "}) == "abc123"
    assert _context_entry_id({"id": "x" * 40}) == "x" * 32


def test_lastfm_signature_matches_the_documented_md5_scheme():
    params = {
        "method": "auth.getSession",
        "api_key": "key-placeholder",
        "token": "tok-placeholder",
        "format": "json",
        "callback": "ignored",
        "api_sig": "ignored",
    }
    payload = "api_keykey-placeholdermethodauth.getSessiontokentok-placeholder" + "s3"
    assert signature(params, "s3") == hashlib.md5(payload.encode("utf-8")).hexdigest()
    assert signature(params, "s3") == "692841ffbe04d2236d3c228e81587eb1"
