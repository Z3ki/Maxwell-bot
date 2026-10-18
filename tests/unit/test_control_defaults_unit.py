"""control_defaults: parsing helpers, capability mapping and catalog shape."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import control_defaults as cd
from control_defaults import (
    DEAD_CONTROL_KEYS,
    DEFAULT_CONTROL,
    GUILD_CAPABILITIES,
    KNOWN_TOOLS,
    audio_input_enabled,
    guild_capability_for_tool,
    parse_bool,
)


@pytest.mark.parametrize("raw", [True, "1", "true", "TRUE", " yes ", "On", 1, "  on\n"])
def test_parse_bool_truthy_spellings(raw):
    assert parse_bool(raw, False) is True


@pytest.mark.parametrize("raw", [False, "0", "false", "No", " OFF ", 0])
def test_parse_bool_falsy_spellings(raw):
    assert parse_bool(raw, True) is False


@pytest.mark.parametrize("raw", ["maybe", "", "2", "enabled", [], {}, 2.5])
@pytest.mark.parametrize("default", [True, False])
def test_parse_bool_unknown_values_fall_back_to_default(raw, default):
    # bool("false") is True; the whole point of parse_bool is to not do that.
    assert parse_bool(raw, default) is default


def test_parse_bool_none_uses_default_and_default_is_false():
    assert parse_bool(None) is False
    assert parse_bool(None, True) is True


def _owner(env=None, control=None, *, with_config=True):
    cfg = SimpleNamespace(ENABLE_AUDIO_INPUT=env) if with_config else None
    return SimpleNamespace(config=cfg, _control=control)


def test_audio_env_off_is_a_hard_off_even_when_dashboard_enables_it():
    assert audio_input_enabled(_owner("false", {"process_audio": True})) is False
    assert audio_input_enabled(_owner(False, {"process_audio": "true"})) is False


def test_audio_dashboard_can_mute_when_env_allows():
    assert audio_input_enabled(_owner("true", {"process_audio": False})) is False
    assert audio_input_enabled(_owner("true", {"process_audio": "on"})) is True


def test_audio_without_dashboard_key_follows_env():
    assert audio_input_enabled(_owner("true", {})) is True
    assert audio_input_enabled(_owner("0", {})) is False


def test_audio_without_config_or_control_is_off():
    assert audio_input_enabled(SimpleNamespace()) is False
    # A control key still decides when no config object is attached.
    assert audio_input_enabled(
        _owner(control={"process_audio": True}, with_config=False)
    )


def test_audio_ignores_non_dict_control():
    owner = _owner("true", ["process_audio"])
    assert audio_input_enabled(owner) is True


@pytest.mark.parametrize(
    "tool, group",
    [
        ("shell", "shell"),
        ("web_search", "web"),
        ("fetch_url", "web"),
        ("see_video", "web"),
        ("create_site", "sites"),
        ("host_file", "sites"),
        ("image_generator", "creative"),
        ("chess_move", "creative"),
        ("checkers_start", "creative"),
        ("send_file", "files"),
        ("ban_member", "moderation"),
        ("purge_messages", "moderation"),
        ("create_thread", "moderation"),
        ("ltm_search", "memory"),
        ("knowledge_graph_query", "memory"),
        ("search_messages", "memory"),
        ("  shell  ", "shell"),
    ],
)
def test_guild_capability_for_tool_known_groups(tool, group):
    assert guild_capability_for_tool(tool) == group


def test_guild_capability_unknown_tool_depends_on_plugin_ownership():
    assert guild_capability_for_tool("lastfm_now_playing") is None
    assert (
        guild_capability_for_tool("lastfm_now_playing", plugin_owned=True) == "plugins"
    )
    assert guild_capability_for_tool("") is None
    assert guild_capability_for_tool(None) is None  # type: ignore[arg-type]


def test_every_capability_group_returned_is_a_declared_group():
    sample = list(KNOWN_TOOLS) + ["anything_else"]
    for name in sample:
        group = guild_capability_for_tool(name, plugin_owned=True)
        assert group in GUILD_CAPABILITIES, name


def test_moderation_tools_all_map_to_moderation():
    for name in cd._GUILD_MODERATION_TOOLS:
        assert guild_capability_for_tool(name) == "moderation", name


def test_known_tools_is_unique_and_keeps_every_fallback_name():
    assert len(KNOWN_TOOLS) == len(set(KNOWN_TOOLS))
    assert set(cd._FALLBACK_KNOWN_TOOLS) <= set(KNOWN_TOOLS)


def test_known_tools_survives_plugin_discovery_failure(monkeypatch):
    import maxwell_core.plugins.catalog as catalog

    def boom():
        raise RuntimeError("manifest scan failed")

    monkeypatch.setattr(catalog, "discover_tool_names", boom)
    out = cd._known_tools()
    assert out == list(dict.fromkeys(cd._FALLBACK_KNOWN_TOOLS))


def test_known_tools_puts_discovered_names_first_without_duplicates(monkeypatch):
    import maxwell_core.plugins.catalog as catalog

    monkeypatch.setattr(
        catalog, "discover_tool_names", lambda: ["new_tool", "shell", "new_tool"]
    )
    out = cd._known_tools()
    assert out[:2] == ["new_tool", "shell"]
    assert out.count("shell") == 1 and out.count("new_tool") == 1


def test_dead_keys_never_overlap_live_defaults():
    assert not (set(DEAD_CONTROL_KEYS) & set(DEFAULT_CONTROL))


def test_retired_prefixes_are_not_live_defaults():
    assert not [k for k in DEFAULT_CONTROL if k.startswith(("autonomy_", "vc_"))]


def test_default_control_scalar_types_are_json_friendly():
    for key, value in DEFAULT_CONTROL.items():
        assert isinstance(value, (bool, int, float, str, list, dict)), key


def test_default_personality_is_a_nonempty_string():
    persona = DEFAULT_CONTROL["base_personality"]
    assert isinstance(persona, str) and persona.strip()


def test_default_limits_are_inside_their_own_sanitizer_bounds():
    # The bot clamps these on load; a default outside its own range would be
    # silently rewritten on first read.
    assert 80 <= DEFAULT_CONTROL["max_response_chars"] <= 8000
    assert 10000 <= DEFAULT_CONTROL["prompt_context_budget"] <= 2_000_000
    assert 256 <= DEFAULT_CONTROL["live_max_output_tokens"] <= 32768
    assert 1 <= DEFAULT_CONTROL["message_quota_limit"] <= 100_000
    assert 60 <= DEFAULT_CONTROL["message_quota_window_seconds"] <= 7 * 24 * 3600
    assert 0 <= DEFAULT_CONTROL["tool_history_messages"] <= 20
    assert 0.0 <= DEFAULT_CONTROL["conversation_watch_pressure"] <= 1.0
