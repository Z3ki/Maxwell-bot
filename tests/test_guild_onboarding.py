"""Member-side onboarding: picking roles/channels in a server.

The submit body is the fussy part — Discord wants a flat list of option
ids and 400s the whole request on anything else, which is how the old
auto-onboard silently left the account with no roles.
"""

import asyncio
from types import SimpleNamespace

import guild_onboarding as gob
from bot_tools import ServerSetupTool

PAYLOAD = {
    "enabled": True,
    "default_channel_ids": ["900"],
    "responses": [],
    "prompts": [
        {
            "id": "p1",
            "title": "What roles do you want?",
            "single_select": False,
            "required": False,
            "in_onboarding": True,
            "options": [
                {
                    "id": "o1",
                    "title": "Announcements",
                    "description": "news pings",
                    "role_ids": ["r1"],
                    "channel_ids": [],
                },
                {
                    "id": "o2",
                    "title": "Model Drops",
                    "description": "",
                    "role_ids": ["r2"],
                    "channel_ids": ["c2"],
                },
            ],
        },
        {
            "id": "p2",
            "title": "Pick a vibe",
            "single_select": True,
            "required": True,
            "in_onboarding": False,
            "options": [
                {
                    "id": "o3",
                    "title": "Chill",
                    "description": "",
                    "role_ids": ["r3"],
                    "channel_ids": [],
                },
                {
                    "id": "o4",
                    "title": "Loud",
                    "description": "",
                    "role_ids": ["r4"],
                    "channel_ids": [],
                },
            ],
        },
    ],
}


def _prompts(**kwargs):
    return gob.normalize_prompts(PAYLOAD, **kwargs)


def test_normalize_keeps_rules_and_drops_optionless_prompts():
    prompts = _prompts()
    assert [p["id"] for p in prompts] == ["p1", "p2"]
    assert prompts[1]["single_select"] and prompts[1]["required"]
    empty = {"prompts": [{"id": "x", "title": "t", "options": []}]}
    assert gob.normalize_prompts(empty) == []


def test_post_join_prompts_can_be_excluded():
    assert [p["id"] for p in _prompts(include_post_join=False)] == ["p1"]


def test_parse_choice_json_tolerates_fences_and_prose():
    text = 'sure!\n```json\n{"picks": [{"prompt_id": "p1", "option_ids": ["o2"]}]}\n```'
    choice = gob.parse_choice_json(text, _prompts())
    # p2 is required, so it gets answered even though the model skipped it.
    assert choice == {"p1": ["o2"], "p2": ["o3"]}


def test_parse_choice_json_returns_empty_on_garbage():
    assert gob.parse_choice_json("no idea sorry", _prompts()) == {}


def test_clamp_drops_unknown_ids_and_enforces_single_select():
    choice = gob.clamp_choice({"p1": ["o1", "nope"], "p2": ["o3", "o4"]}, _prompts())
    assert choice == {"p1": ["o1"], "p2": ["o3"]}


def test_clamp_skips_prompts_the_server_never_offered():
    assert gob.clamp_choice({"ghost": ["o1"]}, _prompts()) == {"p2": ["o3"]}


def test_payload_sends_flat_option_ids():
    # {"prompt_id": ..., "option_ids": [...]} objects get a 400 from Discord:
    # "Value {...} is not snowflake".
    payload = gob.build_payload(
        {"p1": ["o1", "o2"], "p2": ["o3"]}, _prompts(), now_ms=7
    )
    assert payload["onboarding_responses"] == ["o1", "o2", "o3"]
    assert payload["onboarding_prompts_seen"] == {"p1": 7, "p2": 7}
    assert payload["onboarding_responses_seen"] == {"o1": 7, "o2": 7, "o3": 7, "o4": 7}


def test_granted_ids_collects_roles_and_channels():
    roles, channels = gob.granted_ids({"p1": ["o1", "o2"]}, _prompts())
    assert roles == ["r1", "r2"]
    assert channels == ["c2"]


def test_answered_option_ids_accepts_both_response_shapes():
    assert gob.answered_option_ids({"responses": ["o1", "o2"]}) == {"o1", "o2"}
    assert gob.answered_option_ids(
        {"responses": [{"prompt_id": "p1", "option_ids": ["o1"]}]}
    ) == {"o1"}
    assert gob.answered_option_ids({}) == set()


def _run_onboarding(reply, **kwargs):
    sent = {}

    async def request(method, path, payload=None, **params):
        if method == "GET":
            return PAYLOAD
        sent["path"] = path
        sent["params"] = params
        sent["payload"] = payload
        return {}

    async def ask_llm(_messages):
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = asyncio.run(
        gob.run_onboarding(request, 1, "Test", ask_llm=ask_llm, **kwargs)
    )
    return result, sent


def test_run_onboarding_submits_the_model_picks():
    result, sent = _run_onboarding(
        '{"picks": [{"prompt_id": "p1", "option_ids": ["o2"]},'
        ' {"prompt_id": "p2", "option_ids": ["o4"]}]}'
    )
    assert result["ok"] and result["picked_by"] == "model"
    assert sent["path"] == "/guilds/{guild_id}/onboarding-responses"
    assert sent["params"] == {"guild_id": 1}
    assert sent["payload"]["onboarding_responses"] == ["o2", "o4"]
    assert result["role_ids"] == ["r2", "r4"]


def test_run_onboarding_falls_back_when_the_model_dies():
    result, sent = _run_onboarding(RuntimeError("provider down"))
    assert result["ok"] and result["picked_by"] == "fallback"
    assert sent["payload"]["onboarding_responses"] == ["o1", "o3"]


def test_dry_run_picks_without_submitting():
    result, sent = _run_onboarding(
        '{"picks": [{"prompt_id": "p1", "option_ids": ["o1"]}]}', dry_run=True
    )
    assert result["ok"] and "would pick" in result["summary"]
    assert sent == {}


def test_submit_failure_is_reported_not_raised():
    async def request(method, path, payload=None, **params):
        if method == "GET":
            return PAYLOAD
        raise RuntimeError("HTTP 400: Invalid Form Body")

    result = asyncio.run(gob.run_onboarding(request, 1, "Test"))
    assert result["ok"] is False
    assert "submit failed" in result["summary"]


def test_disabled_and_promptless_servers_are_no_ops():
    async def disabled(method, path, payload=None, **params):
        return {"enabled": False, "prompts": []}

    result = asyncio.run(gob.run_onboarding(disabled, 1, "Test"))
    assert result["ok"] is False and "no onboarding" in result["summary"]


def test_picker_messages_carry_ids_and_preferences():
    messages = gob.build_picker_messages(
        "Test", _prompts(), set(), personality="you are a cat", preferences="ai only"
    )
    user = messages[1]["content"]
    assert "o1" in user and "Announcements" in user
    assert "ai only" in user
    assert "you are a cat" in messages[0]["content"]


# --- the server_setup tool itself ------------------------------------------


def _fake_guild():
    return SimpleNamespace(
        id=1,
        name="mehh.fun",
        get_role=lambda rid: SimpleNamespace(name=f"role{rid}"),
        get_channel=lambda cid: SimpleNamespace(name=f"chan{cid}"),
    )


def _setup_tool(result):
    captured = {}

    class FakeBot:
        guilds = [_fake_guild()]

        async def _auto_onboard(self, guild, **kwargs):
            captured.update(kwargs)
            captured["guild"] = guild
            return result

    return ServerSetupTool(FakeBot()), captured


def test_server_setup_reports_picks_and_roles():
    result = {
        "ok": True,
        "summary": "onboarding completed (model): roles -> Model Drops",
        "prompts": _prompts(),
        "choice": {"p1": ["o2"]},
        "role_ids": ["7"],
        "channel_ids": ["8"],
        "picked_by": "model",
    }
    tool, captured = _setup_tool(result)
    message = SimpleNamespace(guild=None, author=SimpleNamespace(id=1))
    out = asyncio.run(tool.execute(message, server="mehh"))
    assert "mehh.fun" in out
    assert "[✓] Model Drops" in out and "[ ] Announcements" in out
    assert "took roles: role7" in out and "took channels: chan8" in out
    assert captured["dry_run"] is False


def test_server_setup_list_only_is_a_dry_run():
    result = {
        "ok": True,
        "summary": "would pick (model): x",
        "prompts": _prompts(),
        "choice": {},
        "role_ids": [],
        "channel_ids": [],
        "picked_by": "model",
    }
    tool, captured = _setup_tool(result)
    message = SimpleNamespace(guild=None, author=SimpleNamespace(id=1))
    # Models pass booleans as strings often enough to matter.
    asyncio.run(tool.execute(message, server="1", list_only="true"))
    assert captured["dry_run"] is True


def test_server_setup_needs_a_server_outside_a_guild():
    tool, _ = _setup_tool({})
    message = SimpleNamespace(guild=None, author=SimpleNamespace(id=1))
    out = asyncio.run(tool.execute(message))
    assert "no server given" in out


def test_server_setup_defaults_to_the_current_guild():
    result = {"ok": True, "summary": "done", "prompts": [], "choice": {}}
    tool, captured = _setup_tool(result)
    guild = _fake_guild()
    message = SimpleNamespace(guild=guild, author=SimpleNamespace(id=1))
    out = asyncio.run(tool.execute(message))
    assert captured["guild"] is guild
    assert "nothing to pick here" in out
