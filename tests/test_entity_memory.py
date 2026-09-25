"""Scoped entity and long-term memory regression tests."""

import asyncio

from rag_memory import MAX_ENTITY_ALIASES, MemoryRequester, RAGMemoryManager


def _run(coro):
    return asyncio.run(coro)


def _requester(user="111", channel="c1", guild="g1", *, is_dm=False, admin=False, public=True):
    return MemoryRequester(
        user_id=user, channel_id=channel, guild_id=guild,
        is_dm=is_dm, is_admin=admin, channel_is_public=public,
    )


def _admin():
    return _requester("owner", "admin-channel", "g1", admin=True)


def _no_embed(mgr):
    def skip(coro):
        coro.close()
        return
    mgr._spawn = skip


def test_identity_aggregation_is_admin_only(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.observe_user("111", "alice", guild_id="g1")
        await mgr.observe_user("111", "alice", guild_id="g2")
        await mgr.observe_user("111", "Al", is_dm=True)
        assert mgr.get_user_entity("111") is None
        assert mgr.list_user_entities() == []
        entity = mgr.get_user_entity("111", requester=_admin())
        assert entity["guild_ids"] == ["g1", "g2"]
        assert entity["dm_seen"] is True
        assert entity["message_count"] == 3
        assert entity["display_names"] == ["Al", "alice"]

    _run(run())


def test_identity_aliases_remain_bounded(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        for i in range(MAX_ENTITY_ALIASES + 5):
            await mgr.observe_user("111", f"name{i}")
        entity = mgr.get_user_entity("111", requester=_admin())
        assert len(entity["display_names"]) == MAX_ENTITY_ALIASES
        assert entity["display_names"][0] == f"name{MAX_ENTITY_ALIASES + 4}"

    _run(run())


def test_entity_fact_isolated_by_guild_user_and_dm(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        await mgr.add_entity_fact(
            "111", "lives in Halifax", requester=_requester(), visibility="shared"
        )
        same_guild_other_public_channel = _requester(channel="c2")
        other_guild = _requester(channel="c3", guild="g2")
        other_user = _requester(user="222", channel="c4")
        dm = _requester(channel="dm-111", guild="", is_dm=True)
        assert [f["content"] for f in await mgr.get_entity_facts(
            "111", requester=same_guild_other_public_channel
        )] == ["lives in Halifax"]
        assert await mgr.get_entity_facts("111", requester=other_guild) == []
        assert await mgr.get_entity_facts("111", requester=other_user) == []
        assert await mgr.get_entity_facts("111", requester=dm) == []
        assert await mgr.get_entity_facts("111") == []

    _run(run())


def test_dm_entity_fact_does_not_enter_guild_memory(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        dm = _requester(channel="dm-111", guild="", is_dm=True)
        await mgr.add_entity_fact(
            "111", "asked about a private trip", requester=dm, visibility="private"
        )
        assert [f["content"] for f in await mgr.get_entity_facts(
            "111", requester=dm
        )] == ["asked about a private trip"]
        assert await mgr.get_entity_facts("111", requester=_requester()) == []

    _run(run())


def test_different_users_can_store_identical_facts(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        a, created_a = await mgr.add_entity_fact(
            "111", "works night shifts", requester=_requester()
        )
        b, created_b = await mgr.add_entity_fact(
            "222", "works night shifts",
            requester=_requester(user="222", channel="c2"),
        )
        assert created_a and created_b and a != b
        assert len(await mgr.get_entity_facts("111", requester=_requester())) == 1
        assert len(await mgr.get_entity_facts(
            "222", requester=_requester(user="222", channel="c2")
        )) == 1

    _run(run())


def test_shared_context_preserves_user_and_dm_scope(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        guild_requester = _requester()
        dm_requester = _requester(channel="dm-111", guild="", is_dm=True)
        await mgr.add_shared_context(
            {
                "content": "prefers tabs",
                "scope": "user:111",
                "visibility": "shared",
                "importance": 7,
            },
            requester=guild_requester,
        )
        await mgr.add_shared_context(
            {
                "content": "asked in a DM",
                "scope": "dm:111",
                "visibility": "private",
                "importance": 6,
            },
            requester=dm_requester,
        )
        assert [f["content"] for f in await mgr.get_entity_facts(
            "111", requester=guild_requester
        )] == ["prefers tabs"]
        assert [f["content"] for f in await mgr.get_entity_facts(
            "111", requester=dm_requester
        )] == ["asked in a DM"]

    _run(run())


def test_entity_ranking_and_budget_are_scoped(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        requester = _requester()
        await mgr.add_entity_fact("111", "trivial", importance=1, requester=requester)
        await mgr.add_entity_fact("111", "identity", importance=10, requester=requester)
        await mgr.add_entity_fact("111", "useful", importance=5, requester=requester)
        assert [f["content"] for f in await mgr.get_entity_facts(
            "111", requester=requester
        )] == ["identity", "useful", "trivial"]
        await mgr.add_entity_fact("111", "a" * 30, importance=10, requester=requester)
        await mgr.add_entity_fact("111", "b" * 30, importance=5, requester=requester)
        kept = await mgr.get_entity_facts("111", budget=40, requester=requester)
        assert [fact["content"][0] for fact in kept] == ["a", "i"]
        assert sum(len(fact["content"]) for fact in kept) <= 40
        assert await mgr.get_entity_facts(
            "111", budget=0, requester=requester
        ) == []

    _run(run())


def test_profile_does_not_inject_cross_guild_aliases(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        await mgr.observe_user("111", "alice", guild_id="g1")
        await mgr.observe_user("111", "Al", guild_id="g2")
        await mgr.add_entity_fact(
            "111", "likes fish", requester=_requester(), visibility="shared"
        )
        profile = await mgr.get_user_profile("111", requester=_requester())
        assert profile["user_id"] == "111"
        assert "display_names" not in profile
        assert [f["content"] for f in profile["facts"]] == ["likes fish"]
        assert await mgr.get_user_profile("111") == {"user_id": "111", "facts": []}

    _run(run())


def test_legacy_unscoped_entity_rows_are_retained_but_hidden(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        fid, created = await mgr.add_entity_fact("111", "legacy note")
        assert created and fid
        assert await mgr.get_entity_facts("111", requester=_requester()) == []
        assert mgr._db.execute(
            "SELECT COUNT(*) FROM vectors WHERE id=?", (fid,)
        ).fetchone()[0] == 1

    _run(run())


def test_ltm_dedupe_and_retrieval_are_scoped(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        requester = _requester()
        first, created_first = await mgr.add_long_term_memory_dedup(
            "the sky is blue", requester=requester
        )
        second, created_second = await mgr.add_long_term_memory_dedup(
            "the sky is blue", requester=requester
        )
        assert created_first and not created_second and first == second
        assert [row["content"] for row in mgr.get_long_term_memory(requester)] == [
            "the sky is blue"
        ]
        assert mgr.get_long_term_memory() == []
        result = await mgr.apply_ltm_batch(
            [
                {"kind": "add", "content": "a brand new fact"},
                {"kind": "add", "content": "a brand new fact"},
            ],
            requester=requester,
        )
        assert result["added"] == 1
        assert result["deduped"] == 1
        assert result["errors"] == 0

    _run(run())


def test_ltm_growth_does_not_evict_at_old_global_cap(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        _no_embed(mgr)
        for index in range(1005):
            await mgr.add_long_term_memory_dedup(f"fact number {index}")
        count = mgr._db.execute(
            "SELECT COUNT(*) FROM vectors WHERE kind='ltm'"
        ).fetchone()[0]
        assert count == 1005
        # Unscoped legacy facts remain available for migration/admin review only.
        assert mgr.get_long_term_memory(_requester()) == []

    _run(run())
