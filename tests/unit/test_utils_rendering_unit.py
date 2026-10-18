"""utils: Discord context rendering, annotations, atomic writes, state store."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

import utils
from utils import (
    JsonStateStore,
    _atomic_json_write_sync,
    _atomic_text_write_sync,
    _describe_component,
    _poll_text,
    _render_message_annotations,
    _truncate,
    format_reactions_annotation,
    message_has_visible_payload,
    message_reference_is_forward,
    render_discord_context_text,
)


def msg(content="", **attrs):
    base = {
        "content": content,
        "attachments": [],
        "embeds": [],
        "stickers": [],
        "components": [],
        "mentions": [],
        "channel_mentions": [],
        "role_mentions": [],
        "guild": None,
        "type": None,
        "created_at": None,
    }
    base.update(attrs)
    return NS(**base)


# -- mentions and timestamps ----------------------------------------------------


def test_render_resolves_user_channel_and_role_mentions():
    alice = NS(id=11, display_name="Alice")
    general = NS(id=22, name="general")
    mods = NS(id=33, name="mods")
    m = msg(
        "hi <@11> and <@!11> in <#22> cc <@&33>",
        mentions=[alice],
        channel_mentions=[general],
        role_mentions=[mods],
    )
    out = render_discord_context_text(m)
    assert out == "hi @Alice(11) and @Alice(11) in #general(22) cc @mods(33)"


def test_render_falls_back_to_guild_known_users_and_unknown_labels():
    guild = NS(
        get_member=lambda uid: NS(display_name="Bob") if uid == 44 else None,
        get_channel=lambda cid: None,
        get_role=lambda rid: None,
    )
    m = msg("<@44> <@55> <@66> <#77> <@&88>", guild=guild)
    out = render_discord_context_text(m, known_users={"55": "Carol"})
    assert out == (
        "@Bob(44) @Carol(55) @unknown-user(66) #unknown-channel(77) @unknown-role(88)"
    )


def test_render_prefixes_utc_timestamp_and_can_omit_it():
    created = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    m = msg("hello", created_at=created)
    assert render_discord_context_text(m) == "[at 2026-03-04 05:06:07 UTC] hello"
    assert render_discord_context_text(m, include_timestamp=False) == "hello"


def test_render_content_override_does_not_mutate_message():
    m = msg("original")
    assert render_discord_context_text(m, content="override") == "override"
    assert m.content == "original"


def test_render_empty_text_returns_only_annotations():
    m = msg("", stickers=[NS(name="wave", format=NS(name="png"))])
    assert render_discord_context_text(m) == "[sticker: wave (png)]"


# -- annotations ----------------------------------------------------------------


def test_poll_text_renders_question_options_and_totals():
    poll = NS(
        question=NS(text="Lunch?"),
        answers=[
            NS(text="Pizza", vote_count=3),
            NS(text=NS(text="Tacos"), vote_count=1),
        ],
        multiple=True,
        total_votes=4,
    )
    assert _poll_text(poll) == (
        "[poll: Lunch? | options: Pizza (3 votes); Tacos (1 votes) "
        "| multiple choice | total votes: 4]"
    )


def test_malformed_poll_does_not_drop_other_annotations():
    class BadPoll:
        @property
        def question(self):
            raise RuntimeError("bad payload")

    m = msg(poll=BadPoll(), stickers=[NS(name="hi", format=None)])
    assert _render_message_annotations(m) == "[sticker: hi]"


@pytest.mark.parametrize(
    "comp, expected",
    [
        (
            NS(options=[NS(label="A"), NS(value="b")], placeholder="Pick"),
            "select Pick: A, b",
        ),
        (NS(options=[], placeholder="Choose"), "select Choose"),
        (
            NS(label="Go", url="https://example.test/x"),
            "link Go <https://example.test/x>",
        ),
        (NS(label="", custom_id="cid", emoji=NS(name="🔥")), "button 🔥"),
        (NS(label="Click"), "button Click"),
        (NS(content="some text block"), "text some text block"),
        (NS(), None),
    ],
)
def test_describe_component(comp, expected):
    assert _describe_component(comp) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.last.fm/api/auth/?api_key=k&token=t",
        "https://last.fm/api/auth/?api_key=k&token=t",
    ],
)
def test_lastfm_approval_urls_never_reach_context(url):
    out = _describe_component(NS(label="Approve", url=url))
    assert out == "link Approve [private approval URL omitted]"
    assert "token" not in out


def test_components_are_deduplicated_through_nested_rows():
    button = NS(label="Same")
    rows = [NS(children=[button, NS(label="Same")]), NS(components=[NS(label="Other")])]
    out = _render_message_annotations(msg(components=rows))
    assert out.count("button Same") == 1 and "button Other" in out


def test_system_thread_call_activity_subscription_purchase_and_flags():
    m = msg(
        type="MessageType.pins_add",
        author=NS(display_name="Dana"),
        channel=NS(name="ideas", parent=NS(name="forum")),
        call=NS(ended_timestamp=1, participants=[NS(display_name="A"), NS(name="B")]),
        activity={"type": 1, "party_id": "p1"},
        role_subscription=NS(
            tier_name="Gold", is_renewal=True, total_months_subscribed=3
        ),
        purchase_notification=NS(guild_product_purchase=NS(product_name="Badge")),
        flags=NS(silent=True, suppress_notifications=True, has_thread=True),
    )
    out = _render_message_annotations(m).splitlines()
    assert out[0].startswith("[system: ") and out[0].endswith("— Dana]")
    assert "[thread: ideas in #forum]" in out
    assert "[call | ended | participants: A, B]" in out
    assert "[message activity: join party p1]" in out
    assert "[role subscription — Gold — renewal — 3 months]" in out
    assert "[purchase: Badge]" in out
    assert "[flags: silent, has thread]" in out


def test_ordinary_message_types_have_no_system_line():
    for kind in ("MessageType.default", "MessageType.reply", None):
        assert "[system:" not in _render_message_annotations(msg(type=kind))


def test_embed_annotation_is_bounded_and_complete():
    embed = NS(
        title="T" * 300,
        description="D" * 500,
        url="https://example.test/page",
        author=NS(name="Writer"),
        fields=[NS(name="k", value="v" * 200)],
        image=None,
        thumbnail="https://example.test/thumb.png",
        video=None,
        footer=NS(text="foot"),
        provider=NS(name="Site"),
        type="rich",
    )
    out = _render_message_annotations(msg(embeds=[embed]), compact=False)
    assert out.startswith("[embed:")
    assert "T" * 200 in out and "T" * 201 not in out
    assert "D" * 400 in out and "D" * 401 not in out
    assert "(by Writer)" in out and "via Site" in out
    assert "k: " + "v" * 160 in out and "v" * 161 not in out
    assert "| footer: foot" in out and "| image: https://example.test/thumb.png" in out


def test_media_urls_in_text_are_classified_and_deduped():
    text = (
        "see https://cdn.example.test/a.png, https://cdn.example.test/a.png "
        "and https://cdn.example.test/clip.mp4 and https://cdn.example.test/v.ogg"
    )
    out = _render_message_annotations(msg(text), raw_content=text)
    assert out.count("[media URL: image https://cdn.example.test/a.png]") == 1
    assert "[media URL: video https://cdn.example.test/clip.mp4]" in out
    assert "[media URL: audio https://cdn.example.test/v.ogg]" in out
    muted = _render_message_annotations(msg(text), raw_content=text, hear_audio=False)
    assert "audio input is off" in muted and "v.ogg" not in muted


def test_forwarded_snapshot_is_rendered_with_origin():
    snap = msg("original words")
    m = msg(
        message_snapshots=[snap],
        reference=NS(guild_id=1, channel_id=2, message_id=3, type=None),
    )
    out = _render_message_annotations(m)
    assert "[forwarded message from server 1 #2 id 3: original words]" in out


@pytest.mark.parametrize(
    "m, forward",
    [
        (msg(reference=None), False),
        (msg(reference=NS(type=None)), False),
        (msg(reference=NS(type=NS(name="forward"))), True),
        (msg(reference=NS(type="MessageReferenceType.forward")), True),
        (msg(reference=NS(type=NS(name="default"))), False),
        (msg(flags=NS(forwarded=True)), True),
    ],
)
def test_message_reference_is_forward(m, forward):
    assert message_reference_is_forward(m) is forward


@pytest.mark.parametrize(
    "attrs, visible",
    [
        ({}, False),
        ({"content": "  "}, False),
        ({"content": "x"}, True),
        ({"attachments": [object()]}, True),
        ({"poll": object()}, True),
        ({"call": object()}, True),
        ({"type": "MessageType.pins_add"}, True),
        ({"message_snapshots": [msg("inner")]}, True),
    ],
)
def test_message_has_visible_payload(attrs, visible):
    assert message_has_visible_payload(msg(**attrs)) is visible


# -- reactions and truncation ---------------------------------------------------


def test_format_reactions_groups_names_and_counts():
    rows = [
        {"emoji": "😂", "user_name": "alice"},
        {"emoji": "😂", "name": "bob"},
        {"emoji": "😂", "user_name": "alice"},
        {"emoji": "👍", "count": 2},
        {"emoji": "🎉", "count": "junk"},
        {"emoji": ""},
        "not a dict",
    ]
    assert format_reactions_annotation(rows) == "[reactions: 😂 alice, bob; 👍×2; 🎉]"
    assert format_reactions_annotation([]) == ""
    assert format_reactions_annotation([{"emoji": " "}]) == ""


@pytest.mark.parametrize(
    "text, budget, expected",
    [
        ("short", 10, "short"),
        ("x" * 50, 30, "x" * 14 + "\n... [truncated]"),
        ("abcdef", 3, "abc"),
        ("abc", "junk", ""),
        ("abc", -5, ""),
    ],
)
def test_truncate_marks_the_cut(text, budget, expected):
    assert _truncate(text, budget) == expected


# -- atomic writes and state store ------------------------------------------------


def test_atomic_writes_replace_and_leave_no_temp_files(tmp_path):
    target = tmp_path / "nested" / "state.json"
    _atomic_json_write_sync(target, {"when": datetime(2026, 1, 1), "ü": 1})
    assert json.loads(target.read_text(encoding="utf-8"))["ü"] == 1
    _atomic_text_write_sync(target, "plain")
    assert target.read_text(encoding="utf-8") == "plain"
    assert sorted(p.name for p in target.parent.iterdir()) == ["state.json"]


def test_atomic_write_failure_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(utils.os, "replace", boom)
    with pytest.raises(OSError):
        _atomic_json_write_sync(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_json_state_store_round_trip_patch_and_log_ring(tmp_path):
    class Store(JsonStateStore):
        log_ring_size = 3

    store = Store(str(tmp_path), state_file="s.json", log_file="l.json")

    async def go():
        assert await store.load_state() == {}
        await store.save_state({"a": 1})
        patched = await store.patch_state({"b": 2})
        for i in range(5):
            await store.append_log_entry({"i": i})
        log = await store.load_log()
        await store.record_error("x" * 3000)
        state = await store.load_state()
        await store.clear_log()
        return patched, log, state, await store.load_log()

    patched, log, state, cleared = asyncio.run(go())
    assert patched == {"a": 1, "b": 2}
    assert [e["i"] for e in log] == [2, 3, 4]
    assert len(state["last_error"]) == 2000
    assert cleared == []


def test_json_state_store_refuses_to_overwrite_corrupt_state(tmp_path):
    store = JsonStateStore(str(tmp_path), state_file="s.json", log_file="l.json")
    (tmp_path / "s.json").write_text("{corrupt", encoding="utf-8")
    (tmp_path / "l.json").write_text('{"entries": "nope"}', encoding="utf-8")

    async def go():
        assert await store.load_state() == {}
        with pytest.raises(json.JSONDecodeError):
            await store.patch_state({"a": 1})
        with pytest.raises(ValueError, match="log entries"):
            await store.append_log_entry({"x": 1})

    asyncio.run(go())
    assert (tmp_path / "s.json").read_text(encoding="utf-8") == "{corrupt"
