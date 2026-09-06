"""Configurable identity: names and IDs come from env/Config, not baked-in people.

Out-of-process Config checks use a blank env file and strip identity-related
variables so a developer's real .env cannot leak into the assertions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from bot import DISCORD_CHAT_PROTOCOL, MAXWELL_BASE_KNOWLEDGE, MaxwellBot
from identity import (
    configured_admin_ids,
    default_wake_words,
    fill_identity,
    identity_values,
    is_partner_persona,
    parse_birthday,
    process_name,
)

_HARDCODED_SNOWFLAKE = "1471821513824014480"

_STRIP_PREFIXES = (
    "REM_",
    "ENABLE_",
    "OLLAMA_",
    "MAXWELL_",
    "DISCORD_",
    "CREATOR_",
    "BOT_",
    "GF_",
    "PARTNER_",
)
_STRIP_KEYS = frozenset(
    {
        "COMMAND_PREFIX",
        "OFFICIAL_INVITE",
        "GF_COMMAND_PREFIX",
    }
)


def _blank_config(**overrides):
    cfg = SimpleNamespace(
        BOT_NAME="Maxwell",
        PARTNER_NAME="Uni",
        CREATOR_NAME="",
        CREATOR_ID="",
        MAXWELL_USER_ID="",
        GF_USER_ID="",
        PARTNER_USER_ID="",
        BOT_PERSONA_TYPE="maxwell",
        BOT_BIRTHDAY="2026-05-21",
        BOT_INVITE_URL="",
        OFFICIAL_INVITE="",
        COMMAND_PREFIX=",",
        GF_COMMAND_PREFIX=".",
        MAXWELL_OWNER_IDS=(),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _run(code: str, env: dict | None = None) -> str:
    """Evaluate a snippet in a fresh interpreter with identity env stripped."""
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_STRIP_PREFIXES) and key not in _STRIP_KEYS
    }
    child_env["MAXWELL_ENV_FILE"] = os.devnull
    child_env.update(env or {})
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_config_identity_defaults_are_empty_without_env():
    """Shipped Config must not inherit a developer's names or Discord IDs."""
    # Child asserts field names only — never prints the actual values.
    out = _run(
        "from config import Config\n"
        "from identity import identity_values\n"
        "bad = []\n"
        "checks = [\n"
        "    ('CREATOR_NAME', Config.CREATOR_NAME, ''),\n"
        "    ('CREATOR_ID', Config.CREATOR_ID, ''),\n"
        "    ('GF_USER_ID', Config.GF_USER_ID, ''),\n"
        "    ('MAXWELL_USER_ID', Config.MAXWELL_USER_ID, ''),\n"
        "    ('BOT_NAME', Config.BOT_NAME, 'Maxwell'),\n"
        "    ('COMMAND_PREFIX', Config.COMMAND_PREFIX, ','),\n"
        "    ('BOT_INVITE_URL', Config.BOT_INVITE_URL, ''),\n"
        "    ('MAXWELL_USAGE_URL', Config.MAXWELL_USAGE_URL, ''),\n"
        "]\n"
        "for name, got, expected in checks:\n"
        "    if got != expected:\n"
        "        bad.append(name)\n"
        "v = identity_values()\n"
        "id_checks = [\n"
        "    ('creator_name', v['creator_name'], ''),\n"
        "    ('creator_id', v['creator_id'], ''),\n"
        "    ('partner_id', v['partner_id'], ''),\n"
        "    ('self_id', v['self_id'], ''),\n"
        "    ('bot_name', v['bot_name'], 'Maxwell'),\n"
        "    ('partner_name', v['partner_name'], 'Uni'),\n"
        "]\n"
        "for name, got, expected in id_checks:\n"
        "    if got != expected:\n"
        "        bad.append(name)\n"
        "print('ok' if not bad else 'non-default:' + ','.join(bad))\n"
    )
    assert out == "ok"


def test_identity_values_empty_owner_fields_on_blank_config(monkeypatch):
    monkeypatch.delenv("MAXWELL_OWNER_IDS", raising=False)
    values = identity_values(_blank_config())
    assert values["creator_name"] == ""
    assert values["creator_id"] == ""
    assert values["self_id"] == ""
    assert values["partner_id"] == ""
    assert values["bot_name"] == "Maxwell"
    assert values["partner_name"] == "Uni"
    assert values["command_prefix"] == ","
    assert values["official_invite"] == ""
    assert values["invite_line"] == ""
    assert "MAXWELL_OWNER_IDS" in values["creator_line"]
    assert _HARDCODED_SNOWFLAKE not in values["creator_id"]
    assert _HARDCODED_SNOWFLAKE not in values["creator_line"]
    assert _HARDCODED_SNOWFLAKE not in values["authority_line"]


def test_identity_values_custom_bot_and_creator(monkeypatch):
    monkeypatch.delenv("MAXWELL_OWNER_IDS", raising=False)
    values = identity_values(
        _blank_config(
            BOT_NAME="Nova",
            CREATOR_NAME="Ada",
            CREATOR_ID="99",
            MAXWELL_USER_ID="1",
            GF_USER_ID="2",
            BOT_INVITE_URL="https://example.com/invite",
        )
    )
    assert values["bot_name"] == "Nova"
    assert values["creator_name"] == "Ada"
    assert values["creator_id"] == "99"
    assert values["self_id"] == "1"
    assert values["partner_id"] == "2"
    assert "Ada" in values["creator_line"]
    assert "99" in values["creator_line"]
    assert "Ada" in values["authority_line"]
    assert "Uni (ID 2)" in values["partner_line"]
    assert "https://example.com/invite" in values["invite_line"]


def test_fill_identity_replaces_bot_name_and_creator_line(monkeypatch):
    monkeypatch.delenv("MAXWELL_OWNER_IDS", raising=False)
    values = identity_values(
        _blank_config(BOT_NAME="Nova", CREATOR_NAME="Ada", CREATOR_ID="99")
    )
    assert fill_identity("You are {bot_name}", values) == "You are Nova"
    filled = fill_identity(
        "You are {bot_name}. {creator_line} {authority_line} {partner_line} {invite_line}",
        values,
    )
    assert "You are Nova" in filled
    assert "Ada" in filled
    assert "99" in filled
    assert "{bot_name}" not in filled
    assert "{creator_line}" not in filled
    assert fill_identity("keep {unknown}", values) == "keep {unknown}"

    knowledge = fill_identity(MAXWELL_BASE_KNOWLEDGE, values)
    assert "You are Nova" in knowledge
    assert "You are Maxwell" not in knowledge
    assert "{bot_name}" not in knowledge
    chat = fill_identity(DISCORD_CHAT_PROTOCOL, values)
    assert "[Nova]" in chat
    assert "[Maxwell]" not in chat


def test_configured_admin_ids_has_no_baked_in_snowflake(monkeypatch):
    monkeypatch.delenv("MAXWELL_OWNER_IDS", raising=False)
    assert configured_admin_ids(_blank_config()) == set()
    assert _HARDCODED_SNOWFLAKE not in configured_admin_ids(_blank_config())
    ids = configured_admin_ids(
        _blank_config(MAXWELL_OWNER_IDS=("10", "20"), CREATOR_ID="30"),
        extra={"40", ""},
    )
    assert ids == {"10", "20", "30", "40"}
    assert _HARDCODED_SNOWFLAKE not in ids


def test_parse_birthday_valid_invalid_and_default(monkeypatch):
    monkeypatch.delenv("BOT_BIRTHDAY", raising=False)
    assert parse_birthday("2024-12-01") == datetime(2024, 12, 1, tzinfo=timezone.utc)
    assert parse_birthday("not-a-date") == datetime(2026, 5, 21, tzinfo=timezone.utc)
    assert parse_birthday("") == datetime(2026, 5, 21, tzinfo=timezone.utc)


def test_default_wake_words_from_bot_name():
    assert default_wake_words("Sparky") == ["sparky"]
    assert default_wake_words("Foo Bar") == ["foo bar", "foo"]


def test_is_partner_persona():
    assert is_partner_persona(_blank_config()) is False
    assert is_partner_persona(_blank_config(BOT_PERSONA_TYPE="gf")) is True
    assert is_partner_persona(persona="mommy") is True
    assert is_partner_persona(persona="luna") is True
    assert is_partner_persona(persona="maxwell") is False


def test_process_name_prefers_bot_then_config():
    assert process_name(SimpleNamespace(bot_name="Sparky")) == "Sparky"
    assert process_name(None, config=_blank_config(BOT_NAME="Nova")) == "Nova"
    blank = SimpleNamespace(bot_name="", config=_blank_config(BOT_NAME="Maxwell"))
    assert process_name(blank) == "Maxwell"


def test_hardcoded_snowflake_is_not_admin_unless_allowlisted(monkeypatch):
    """_is_admin must not treat a baked-in Discord id as owner by itself."""
    monkeypatch.delenv("MAXWELL_OWNER_IDS", raising=False)
    monkeypatch.delenv("CREATOR_ID", raising=False)
    bot = SimpleNamespace(_admins=set())
    is_admin = MaxwellBot._is_admin.__get__(bot)
    assert is_admin(_HARDCODED_SNOWFLAKE) is False
    assert is_admin(int(_HARDCODED_SNOWFLAKE)) is False
