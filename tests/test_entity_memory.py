"""Global per-user entity memory.

The premise: a Discord user id is already global — the same person in two
servers and a DM is one id — but nothing in the store used that. Channel
memory is per-channel, LTM is unattributed, and shared-context facts are
scoped strings. So the bot could learn your name in one server and meet you
as a stranger in the next. These tests pin the properties that make that
false, and the write-time dedup that keeps a re-asserted fact from being
reported as a failure.
"""

import asyncio

from rag_memory import MAX_ENTITY_ALIASES, RAGMemoryManager


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_identity_accumulates_across_guilds_and_dms(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.observe_user("111", "alice", guild_id="g1")
        await mgr.observe_user("111", "alice", guild_id="g2")
        await mgr.observe_user("111", "Al", is_dm=True)

        entity = mgr.get_user_entity("111")
        assert entity["guild_ids"] == ["g1", "g2"]
        assert entity["dm_seen"] is True
        assert entity["message_count"] == 3
        # Newest name first: the current alias is what identifies them now,
        # the older ones are what make them recognisable.
        assert entity["display_names"] == ["Al", "alice"]

    _run(run())


def test_alias_list_is_bounded(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        for i in range(MAX_ENTITY_ALIASES + 5):
            await mgr.observe_user("111", f"name{i}")
        names = mgr.get_user_entity("111")["display_names"]
        assert len(names) == MAX_ENTITY_ALIASES
        assert names[0] == f"name{MAX_ENTITY_ALIASES + 4}"

    _run(run())


def test_observing_an_unknown_user_is_a_no_op(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.observe_user("", "nobody")
        assert mgr.list_user_entities() == []
        assert mgr.get_user_entity("") is None
        assert mgr.get_user_entity("nope") is None

    _run(run())


def test_the_same_fact_twice_is_a_dedup_not_a_second_row(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        first, created_first = await mgr.add_entity_fact("111", "works night shifts")
        second, created_second = await mgr.add_entity_fact(
            "111", "works night shifts", importance=9
        )
        assert created_first is True
        assert created_second is False
        assert first == second
        facts = await mgr.get_entity_facts("111")
        assert len(facts) == 1
        # A re-assertion raises the fact's importance rather than being lost.
        assert facts[0]["importance"] == 9

    _run(run())


def test_two_people_can_hold_the_same_fact(tmp_path):
    # The unique index is (kind, channel_id, content_hash) and every entity row
    # has channel_id=''. Hashing the bare text would make "works night shifts"
    # collide between different humans and silently drop the second person's.
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        a, created_a = await mgr.add_entity_fact("111", "works night shifts")
        b, created_b = await mgr.add_entity_fact("222", "works night shifts")
        assert created_a and created_b
        assert a != b
        assert len(await mgr.get_entity_facts("111")) == 1
        assert len(await mgr.get_entity_facts("222")) == 1

    _run(run())


def test_facts_are_not_scoped_to_the_guild_they_were_learned_in(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.observe_user("111", "alice", guild_id="g1")
        await mgr.add_entity_fact("111", "lives in Halifax", source_guild_id="g1")
        # No guild argument exists on the read path at all — that is the point.
        facts = await mgr.get_entity_facts("111")
        assert [f["content"] for f in facts] == ["lives in Halifax"]

    _run(run())


def test_existing_user_scoped_shared_context_is_folded_in(tmp_path):
    # Upgrading should not start every profile from empty: `user:<id>` and
    # `dm:<id>` shared-context rows are the same tier under an older name.
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_shared_context(
            {"content": "prefers tabs", "scope": "user:111", "importance": 7}
        )
        await mgr.add_shared_context(
            {"content": "asked in a DM", "scope": "dm:111", "importance": 6}
        )
        await mgr.add_shared_context(
            {"content": "someone else's", "scope": "user:222", "importance": 9}
        )
        contents = [f["content"] for f in await mgr.get_entity_facts("111")]
        assert "prefers tabs" in contents
        assert "asked in a DM" in contents
        assert "someone else's" not in contents

        excluded = await mgr.get_entity_facts("111", include_shared_context=False)
        assert excluded == []

    _run(run())


def test_facts_rank_by_importance_then_recency(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_entity_fact("111", "trivial", importance=1)
        await mgr.add_entity_fact("111", "identity", importance=10)
        await mgr.add_entity_fact("111", "useful", importance=5)
        assert [f["content"] for f in await mgr.get_entity_facts("111")] == [
            "identity",
            "useful",
            "trivial",
        ]

    _run(run())


def test_the_budget_trims_from_the_tail(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_entity_fact("111", "a" * 30, importance=10)
        await mgr.add_entity_fact("111", "b" * 30, importance=5)
        kept = await mgr.get_entity_facts("111", budget=40)
        assert [f["content"][0] for f in kept] == ["a"]
        # A budget of zero means the tier is off, not "one item anyway".
        assert await mgr.get_entity_facts("111", budget=0) == []

    _run(run())


def test_a_fact_creates_the_person(tmp_path):
    # A fact can arrive before the person has ever spoken (an admin note, an
    # extraction from someone else's message). They must still show up.
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_entity_fact("333", "runs the server")
        assert [e["user_id"] for e in mgr.list_user_entities()] == ["333"]
        assert mgr.get_user_entity("333")["fact_count"] == 1
        assert mgr.entity_stats() == {"users": 1, "facts": 1, "facts_embedded": 0}

    _run(run())


def test_empty_writes_are_refused(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        assert await mgr.add_entity_fact("", "something") == ("", False)
        assert await mgr.add_entity_fact("111", "   ") == ("", False)
        assert await mgr.get_entity_facts("") == []

    _run(run())


def test_facts_can_be_removed(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        fid, _ = await mgr.add_entity_fact("111", "temporary")
        assert await mgr.remove_entity_fact(fid) is True
        assert await mgr.remove_entity_fact(fid) is False
        assert await mgr.get_entity_facts("111") == []

    _run(run())


def test_profile_bundles_identity_and_facts(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.observe_user("111", "alice", guild_id="g1")
        await mgr.add_entity_fact("111", "likes fish")
        profile = await mgr.get_user_profile("111")
        assert profile["display_names"] == ["alice"]
        assert [f["content"] for f in profile["facts"]] == ["likes fish"]

    _run(run())


# ─── write-time dedup for plain LTM ─────────────────────────────────────


def test_repeating_an_ltm_line_is_a_dedup_not_an_error(tmp_path):
    # The unique index always refused the duplicate; it refused it by raising,
    # which apply_ltm_batch counted as an error — and models retry failures.
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        first, created_first = await mgr.add_long_term_memory_dedup("the sky is blue")
        second, created_second = await mgr.add_long_term_memory_dedup("the sky is blue")
        assert created_first is True
        assert created_second is False
        assert first == second
        assert len(mgr.get_long_term_memory()) == 1

        result = await mgr.apply_ltm_batch(
            [
                {"kind": "add", "content": "a brand new fact"},
                {"kind": "add", "content": "a brand new fact"},
            ]
        )
        assert result["added"] == 1
        assert result["deduped"] == 1
        assert result["errors"] == 0

    _run(run())
