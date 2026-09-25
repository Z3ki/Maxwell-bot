"""Privacy and scope regression tests for semantic memory retrieval."""

import asyncio
import time

import numpy as np

from rag_memory import (
    EMBED_DIM,
    MemoryRequester,
    RAGMemoryManager,
    _embedding_to_blob,
)


def _run(coro):
    return asyncio.run(coro)


def _requester(user="u1", channel="c1", guild="g1", *, is_dm=False, admin=False, public=True):
    return MemoryRequester(
        user_id=user, channel_id=channel, guild_id=guild,
        is_dm=is_dm, is_admin=admin, channel_is_public=public,
    )


def _insert_message(mgr, *, mid, guild, channel, author, content, vector):
    mgr._db.execute(
        "INSERT INTO vectors (id, kind, channel_id, guild_id, author, author_id, "
        "source, content, content_hash, embedding, metadata, scope, importance, "
        "parent_id, chunk_index, downvotes, timestamp, created_at) "
        "VALUES (?, 'message', ?, ?, ?, ?, 'user', ?, ?, ?, '{}', '', 0, '', 0, 0, ?, ?)",
        (
            mid, channel, guild, author, author, content, mid,
            _embedding_to_blob(vector), "2026-01-01T00:00:00+00:00", time.time(),
        ),
    )


def test_channel_history_isolated_even_when_channel_ids_match(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_to_channel_memory("same-channel", {
            "message_id": "g1-msg", "author": "u1", "author_id": "u1",
            "guild_id": "g1", "content": "guild one text",
        })
        await mgr.add_to_channel_memory("same-channel", {
            "message_id": "g2-msg", "author": "u2", "author_id": "u2",
            "guild_id": "g2", "content": "guild two secret",
        })
        visible = await mgr.get_channel_memory(
            "same-channel", requester=_requester(channel="same-channel", guild="g1")
        )
        assert [row["content"] for row in visible] == ["guild one text"]
        assert await mgr.get_channel_memory(
            "same-channel", requester=_requester(channel="same-channel", guild="g3")
        ) == []

    _run(run())


def test_rag_search_filters_by_requester_before_scoring_vectors(tmp_path, monkeypatch):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        vector = np.zeros(EMBED_DIM, dtype=np.float32)
        vector[0] = 1.0
        _insert_message(
            mgr, mid="g1", guild="g1", channel="same", author="u1",
            content="guild one memory", vector=vector,
        )
        _insert_message(
            mgr, mid="g2", guild="g2", channel="same", author="u2",
            content="guild two secret", vector=vector,
        )

        async def query_embedding(_query):
            return vector
        monkeypatch.setattr(mgr, "_embed_for_query", query_embedding)

        requester = _requester(channel="same", guild="g1")
        rows = await mgr.rag_search(
            "memory", kinds=["message"], channel_id="same", guild_id="g1",
            requester=requester, min_similarity=0.1,
        )
        assert [row["content"] for row in rows] == ["guild one memory"]
        assert await mgr.rag_search(
            "memory", kinds=["message"], channel_id="other",
            requester=requester, min_similarity=0.1,
        ) == []

    _run(run())


def test_shared_facts_do_not_cross_guild_user_or_channel_scopes(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        source = _requester(user="u1", channel="public-source", guild="g1")
        await mgr.add_shared_context(
            {
                "content": "shared in one server",
                "scope": "user:u1",
                "visibility": "shared",
                "importance": 7,
            },
            requester=source,
        )
        same_user_public_channel = _requester(
            user="u1", channel="public-destination", guild="g1"
        )
        other_guild = _requester(user="u1", channel="channel-g2", guild="g2")
        other_user = _requester(user="u2", channel="public-destination", guild="g1")
        assert [row["content"] for row in await mgr.get_relevant_shared_context(
            requester=same_user_public_channel,
            user_id="u1", guild_id="g1", channel_id="public-destination",
        )] == ["shared in one server"]
        assert await mgr.get_relevant_shared_context(
            requester=other_guild, user_id="u1", guild_id="g2",
            channel_id="channel-g2",
        ) == []
        assert await mgr.get_relevant_shared_context(
            requester=other_user, user_id="u2", guild_id="g1",
            channel_id="public-destination",
        ) == []

    _run(run())


def test_dm_memory_is_not_retrievable_from_a_guild(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        dm = _requester(user="u1", channel="dm-u1", guild="", is_dm=True)
        await mgr.add_shared_context(
            {
                "content": "private DM fact",
                "scope": "dm:u1",
                "visibility": "private",
                "importance": 8,
            },
            requester=dm,
        )
        assert [entry["content"] for entry in await mgr.get_relevant_shared_context(
            requester=dm, user_id="u1", is_dm=True, channel_id="dm-u1"
        )] == ["private DM fact"]
        guild = _requester(user="u1", channel="public", guild="g1")
        assert await mgr.get_relevant_shared_context(
            requester=guild, user_id="u1", guild_id="g1", channel_id="public"
        ) == []

    _run(run())


def test_ambiguous_legacy_facts_stay_stored_but_are_not_prompt_visible(tmp_path, monkeypatch):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))

        async def query_embedding(_query):
            return np.ones(EMBED_DIM, dtype=np.float32)

        monkeypatch.setattr(mgr, "_embed_for_query", query_embedding)
        # Simulate an old row migrated from storage with no reliable source scope.
        mgr._db.execute(
            "INSERT INTO vectors (id, kind, content, scope, importance, metadata, "
            "timestamp, created_at) VALUES (?, 'shared_context', ?, 'global', 10, '{}', ?, ?)",
            ("legacy-1", "legacy ambiguous", "2025-01-01T00:00:00+00:00", time.time()),
        )
        count = mgr._db.execute(
            "SELECT COUNT(*) FROM vectors WHERE kind='shared_context'"
        ).fetchone()[0]
        assert count == 1
        assert await mgr.get_relevant_shared_context(
            requester=_requester(), user_id="u1", guild_id="g1", channel_id="c1"
        ) == []
        assert await mgr.rag_search(
            "legacy", kinds=["shared_context"], requester=_requester()
        ) == []

    _run(run())



def test_administrator_context_does_not_widen_guild_user_or_channel_scope(tmp_path, monkeypatch):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        vector = np.zeros(EMBED_DIM, dtype=np.float32)
        vector[0] = 1.0
        _insert_message(
            mgr, mid="same-guild", guild="g1", channel="same", author="u1",
            content="same channel conversation", vector=vector,
        )
        _insert_message(
            mgr, mid="other-guild", guild="g2", channel="same", author="u2",
            content="unrelated server conversation", vector=vector,
        )

        async def query_embedding(_query):
            return vector

        monkeypatch.setattr(mgr, "_embed_for_query", query_embedding)
        admin = _requester(user="operator", channel="same", guild="g1", admin=True)
        history = await mgr.get_channel_memory("same", requester=admin)
        assert [row["content"] for row in history] == ["same channel conversation"]

        rows = await mgr.rag_search(
            "conversation", kinds=["message"], requester=admin, min_similarity=0.1
        )
        assert [row["content"] for row in rows] == ["same channel conversation"]
        assert await mgr.rag_search(
            "conversation", kinds=["message"], guild_id="g2", requester=admin
        ) == []
        assert await mgr.rag_search(
            "conversation", kinds=["message"], author_id="u2", requester=admin
        ) == []
        assert await mgr.get_relevant_shared_context(
            requester=admin, user_id="u2", guild_id="g2", channel_id="same"
        ) == []
        assert await mgr.get_entity_facts("u2", requester=admin) == []

    _run(run())


def test_global_admin_recall_returns_only_operator_approved_public_facts(tmp_path, monkeypatch):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        vector = np.zeros(EMBED_DIM, dtype=np.float32)
        vector[0] = 1.0
        async def query_embedding(_query):
            return vector
        monkeypatch.setattr(mgr, "_embed_for_query", query_embedding)

        mgr._db.execute(
            "INSERT INTO vectors (id, kind, content, embedding, scope, metadata, "
            "timestamp, created_at) VALUES (?, 'ltm', ?, ?, 'global', ?, ?, ?)",
            (
                "public-fact",
                "operator approved public fact",
                _embedding_to_blob(vector),
                '{"visibility": "public", "public_approved": true, '
                '"source_kind": "operator_public", "source_user_id": "operator", '
                '"source_channel_id": "operator-api"}',
                "2026-01-01T00:00:00+00:00",
                time.time(),
            ),
        )
        _insert_message(
            mgr, mid="private-chat", guild="g2", channel="private-c2", author="u2",
            content="private server conversation", vector=vector,
        )

        admin = _requester(user="operator", channel="c1", guild="g1", admin=True)
        rows = await mgr.rag_search(
            "fact", kinds=["ltm", "message"], requester=admin,
            global_public_only=True, min_similarity=0.1,
        )
        assert [row["id"] for row in rows] == ["public-fact"]
        assert await mgr.rag_search(
            "fact", kinds=["ltm", "message"],
            requester=_requester(user="u1", channel="c1", guild="g1"),
            global_public_only=True, min_similarity=0.1,
        ) == []

    _run(run())


def test_unscoped_retrieval_interfaces_fail_closed(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_to_channel_memory("c1", {
            "message_id": "m1", "author": "u1", "author_id": "u1",
            "guild_id": "g1", "content": "private history",
        })
        assert await mgr.get_channel_memory("c1") == []
        assert await mgr.rag_search("private", kinds=["message"]) == []
        assert await mgr.get_relevant_shared_context(user_id="u1") == []
        assert await mgr.get_entity_facts("u1") == []
        assert await mgr.get_user_profile("u1") == {"user_id": "u1", "facts": []}
        assert mgr.get_long_term_memory() == []

    _run(run())

def test_identical_messages_keep_identity_and_replay_keeps_chronology(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))

        def skip_background(coro):
            coro.close()
            return

        mgr._spawn = skip_background
        messages = [
            {
                "message_id": "old-message",
                "author": "u1",
                "author_id": "u1",
                "guild_id": "g1",
                "content": "same text",
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
            {
                "message_id": "new-message",
                "author": "u2",
                "author_id": "u2",
                "guild_id": "g1",
                "content": "same text",
                "timestamp": "2026-01-02T00:00:00+00:00",
            },
        ]
        await mgr.add_to_channel_memory("c1", messages[0])
        await mgr.add_to_channel_memory("c1", messages[1])
        # A duplicate Gateway delivery replaces the same Discord identity.
        await mgr.add_to_channel_memory("c1", messages[0])
        rows = await mgr.get_channel_memory(
            "c1", requester=_requester(channel="c1", guild="g1")
        )
        assert [row["message_id"] for row in rows] == [
            "old-message", "new-message"
        ]
        assert [row["content"] for row in rows] == ["same text", "same text"]
        assert mgr._db.execute(
            "SELECT COUNT(*) FROM vectors WHERE kind='message' AND channel_id='c1'"
        ).fetchone()[0] == 2
        await mgr.flush()
        mgr._db.close()

    _run(run())

