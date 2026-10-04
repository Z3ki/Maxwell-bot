"""Memory regression coverage with local SQLite and deterministic embeddings."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import numpy as np
import pytest

import rag_memory
from maxwell_core.memory.embeddings import extract_embeddings
from maxwell_core.memory.identity import migrate_scoped_hashes
from rag_memory import (
    EMBED_DIM,
    MemoryRequester,
    RAGMemoryManager,
    RemEventLog,
    _blob_to_embedding,
    _embedding_to_blob,
)


def requester(user="alice", channel="room", guild="guild", *, dm=False, admin=False):
    return MemoryRequester(
        user_id=user,
        channel_id=channel,
        guild_id=guild,
        is_dm=dm,
        is_admin=admin,
        channel_is_public=not dm,
    )


@pytest.fixture
def memory_factory(tmp_path, monkeypatch):
    """Every test owns its DB and cannot contact a live embedding endpoint."""
    managers = []

    def skip_background(_manager, coroutine):
        coroutine.close()

    monkeypatch.setattr(RAGMemoryManager, "_spawn", skip_background)
    monkeypatch.setattr(RAGMemoryManager, "_embed", AsyncMock(return_value=None))

    def create(*, directory="memory", max_messages=10000):
        manager = RAGMemoryManager(str(tmp_path / directory), max_messages=max_messages)
        managers.append(manager)
        return manager

    yield create
    for manager in managers:
        manager._db.close()


@pytest.mark.parametrize(
    "other",
    [
        requester(user="bob"),
        requester(guild="other-guild"),
        requester(guild="", dm=True),
    ],
)
def test_identical_ltm_facts_keep_each_private_source(memory_factory, other):
    async def run():
        memory = memory_factory()
        first = requester()
        a, created_a = await memory.add_long_term_memory_dedup(
            "same fact", requester=first
        )
        b, created_b = await memory.add_long_term_memory_dedup(
            "same fact", requester=other
        )
        assert created_a and created_b and a != b
        assert [row["id"] for row in memory.get_long_term_memory(first)] == [a]
        assert [row["id"] for row in memory.get_long_term_memory(other)] == [b]
        again, created = await memory.add_long_term_memory_dedup(
            "same fact", requester=other
        )
        assert again == b and not created

    asyncio.run(run())


@pytest.mark.parametrize(
    "other",
    [
        requester(guild="other-guild"),
        requester(channel="private-room"),
        requester(channel="dm-alice", guild="", dm=True),
    ],
)
def test_identical_entity_facts_keep_each_source(memory_factory, other):
    async def run():
        memory = memory_factory()
        first = requester()
        a, created_a = await memory.add_entity_fact(
            "alice", "same fact", requester=first
        )
        b, created_b = await memory.add_entity_fact(
            "alice", "same fact", requester=other
        )
        assert created_a and created_b and a != b
        for source, expected in ((first, a), (other, b)):
            rows = await memory.get_entity_facts(
                "alice", requester=source, include_shared_context=False
            )
            assert [row["id"] for row in rows] == [expected]

    asyncio.run(run())


@pytest.mark.parametrize(
    "other",
    [
        requester(user="bob"),
        requester(guild="other-guild"),
        requester(channel="private-room"),
    ],
)
def test_shared_context_dedup_keeps_provenance_and_original_ids(memory_factory, other):
    async def run():
        memory = memory_factory()
        first = requester()
        # Channel scope exercises different users in one room, while user
        # scope exercises one user's independent rooms/servers.
        scope = "channel:room" if other.user_id != first.user_id else "user:alice"
        entry = {"scope": scope, "content": "same fact", "visibility": "private"}
        a = await memory.add_shared_context(entry, requester=first)
        b = await memory.add_shared_context(entry, requester=other)
        assert a and b and a != b
        assert await memory.add_shared_context(entry, requester=first) == a
        for source, expected in ((first, a), (other, b)):
            rows = await memory.get_relevant_shared_context(requester=source)
            assert [row["id"] for row in rows] == [expected]

    asyncio.run(run())


def test_repeating_a_fact_cannot_promote_its_private_source(memory_factory):
    async def run():
        memory = memory_factory()
        source = requester()
        private = await memory.add_shared_context(
            {"content": "same fact", "visibility": "private"}, requester=source
        )
        shared = await memory.add_shared_context(
            {"content": "same fact", "visibility": "shared"}, requester=source
        )
        assert private != shared
        original = memory._db.execute(
            "SELECT metadata FROM vectors WHERE id=?", (private,)
        ).fetchone()
        assert json.loads(original["metadata"])["visibility"] == "private"
        assert [
            row["id"]
            for row in await memory.get_relevant_shared_context(
                requester=requester(channel="second-room")
            )
        ] == [shared]

    asyncio.run(run())


@pytest.mark.parametrize("target_kind", ["shared_context", "message"])
@pytest.mark.parametrize("admin", [False, True])
def test_shared_context_id_cannot_replace_another_owner_or_record_type(
    memory_factory, target_kind, admin
):
    async def run():
        memory = memory_factory()
        owner = requester()
        if target_kind == "shared_context":
            target = await memory.add_shared_context(
                {"content": "owner fact"}, requester=owner
            )
        else:
            target = "discord-message"
            await memory.add_to_channel_memory(
                "room",
                {
                    "message_id": target,
                    "guild_id": "guild",
                    "author_id": "alice",
                    "content": "owner fact",
                },
            )
        before = dict(
            memory._db.execute("SELECT * FROM vectors WHERE id=?", (target,)).fetchone()
        )
        result = await memory.add_shared_context(
            {"id": target, "content": "replacement"},
            requester=requester(user="bob", admin=admin),
        )
        if admin and target_kind == "shared_context":
            assert result == target  # Operators can explicitly replace facts.
        else:
            assert result == ""
            after = dict(
                memory._db.execute(
                    "SELECT * FROM vectors WHERE id=?", (target,)
                ).fetchone()
            )
            assert after == before

    asyncio.run(run())


def test_ltm_batch_cannot_edit_or_delete_another_private_source(memory_factory):
    async def run():
        memory = memory_factory()
        victim = await memory.add_long_term_memory(
            "private fact", requester=requester()
        )
        result = await memory.apply_ltm_batch(
            [
                {"kind": "edit", "id": victim, "content": "tampered"},
                {"kind": "delete", "id": victim},
            ],
            requester=requester(user="bob", admin=True),
        )
        assert result["errors"] == 2
        assert result["edited"] == result["deleted"] == 0
        assert memory.get_long_term_memory(requester())[0]["content"] == "private fact"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["ltm", "shared_context"])
def test_edit_into_duplicate_fails_without_losing_either_row(memory_factory, kind):
    async def run():
        memory = memory_factory()
        source = requester(admin=True)
        if kind == "ltm":
            a = await memory.add_long_term_memory("first", requester=source)
            b = await memory.add_long_term_memory("second", requester=source)
            changed = await memory.edit_long_term_memory(b, "first")
        else:
            a = await memory.add_shared_context({"content": "first"}, requester=source)
            b = await memory.add_shared_context({"content": "second"}, requester=source)
            changed = await memory.update_shared_context(
                b, {"content": "first"}, requester=source
            )
        assert not changed
        rows = memory._db.execute(
            "SELECT id, content FROM vectors WHERE kind=?", (kind,)
        ).fetchall()
        assert {row["id"]: row["content"] for row in rows} == {a: "first", b: "second"}

    asyncio.run(run())


@pytest.mark.parametrize("raw_metadata", ["[]", '"scalar"', "null", "42", "{broken"])
def test_non_object_or_corrupt_metadata_does_not_break_reopen(
    memory_factory, raw_metadata
):
    memory = memory_factory()
    memory._db.execute(
        "INSERT INTO vectors (id, kind, content, metadata, timestamp, created_at) "
        "VALUES ('legacy', 'ltm', 'legacy fact', ?, '', 1)",
        (raw_metadata,),
    )
    reopened = memory_factory()
    assert reopened.get_long_term_memory(requester()) == []
    row = reopened._db.execute(
        "SELECT metadata FROM vectors WHERE id='legacy'"
    ).fetchone()
    assert row["metadata"] == raw_metadata


def test_scope_hash_migration_preserves_all_rows_and_is_idempotent(memory_factory):
    async def run():
        memory = memory_factory()
        ids = []
        for user in ("alice", "bob"):
            ids.append(
                await memory.add_long_term_memory(
                    "same fact", requester=requester(user=user)
                )
            )
            ids.append(
                await memory.add_shared_context(
                    {"content": "same fact", "scope": "channel:room"},
                    requester=requester(user=user),
                )
            )
        memory._db.execute("DROP INDEX idx_unique_fact_content")
        memory._db.execute("DELETE FROM memory_migrations")
        # Simulate historical unsalted hashes. Their colliding source rows
        # must survive the upgrade, including SQLite rowid and cached vectors.
        digest = hashlib.sha256(b"same fact").hexdigest()
        memory._db.execute(
            "UPDATE vectors SET content_hash=?, embedding=?",
            (digest, _embedding_to_blob(np.ones(EMBED_DIM))),
        )
        before = [
            dict(row)
            for row in memory._db.execute("SELECT rowid, * FROM vectors ORDER BY id")
        ]
        reopened = memory_factory()
        after = [
            dict(row)
            for row in reopened._db.execute("SELECT rowid, * FROM vectors ORDER BY id")
        ]
        assert {row["id"] for row in after} == set(ids)
        for old, new in zip(before, after):
            assert {k: v for k, v in old.items() if k != "content_hash"} == {
                k: v for k, v in new.items() if k != "content_hash"
            }
        assert len({row["content_hash"] for row in after}) == 4
        again = memory_factory()
        assert [
            dict(row)
            for row in again._db.execute("SELECT rowid, * FROM vectors ORDER BY id")
        ] == after
        for user in ("alice", "bob"):
            assert len(again.get_long_term_memory(requester(user=user))) == 1
            duplicate = await again.add_shared_context(
                {"content": "same fact", "scope": "channel:room"},
                requester=requester(user=user),
            )
            assert duplicate in ids
        assert again._count_all() == 4

    asyncio.run(run())


def test_scope_hash_migration_rolls_back_every_write_on_failure(memory_factory):
    memory = memory_factory()
    asyncio.run(memory.add_long_term_memory("fact", requester=requester()))
    memory._db.execute("UPDATE vectors SET content_hash='old-hash'")
    memory._db.execute("DELETE FROM memory_migrations")
    before = [dict(row) for row in memory._db.execute("SELECT * FROM vectors")]

    def fails(_content):
        raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected"):
        migrate_scoped_hashes(memory._db, fails)
    assert [dict(row) for row in memory._db.execute("SELECT * FROM vectors")] == before
    assert memory._db.execute(
        "SELECT name FROM sqlite_master WHERE name='idx_unique_fact_content'"
    ).fetchone()
    assert not memory._db.in_transaction


def test_completed_hash_migration_performs_no_vector_updates_on_reopen(
    memory_factory, monkeypatch
):
    async def run():
        memory = memory_factory()
        await memory.add_long_term_memory("fact", requester=requester())
        statements = []
        original = RAGMemoryManager._init_db

        def traced_init(manager):
            original(manager)
            manager._db.set_trace_callback(statements.append)

        monkeypatch.setattr(RAGMemoryManager, "_init_db", traced_init)
        reopened = memory_factory()
        migrate_scoped_hashes(
            reopened._db, lambda _text: pytest.fail("rehash on second open")
        )
        assert not any(
            statement.startswith("UPDATE vectors") for statement in statements
        )
        assert reopened.get_long_term_memory(requester())[0]["content"] == "fact"

    asyncio.run(run())


def test_malformed_legacy_web_metadata_does_not_repeat_hash_repairs(memory_factory):
    memory = memory_factory()
    memory._db.execute(
        "INSERT INTO vectors (id, kind, content, metadata, timestamp, created_at) "
        "VALUES ('missing-web-url', 'web_result', 'historical text', '{}', '', 1)"
    )
    migrate_scoped_hashes(memory._db, lambda text: text)
    row = memory._db.execute(
        "SELECT content, metadata, content_hash FROM vectors WHERE id='missing-web-url'"
    ).fetchone()
    assert row["content"] == "historical text" and row["metadata"] == "{}"
    assert row["content_hash"]
    migrate_scoped_hashes(
        memory._db, lambda _text: pytest.fail("completed repair was repeated")
    )


@pytest.mark.parametrize(
    "sources",
    [
        (requester(guild="one"), requester(guild="two")),
        (
            requester(channel="dm-one", guild="", dm=True),
            requester(channel="dm-two", guild="", dm=True),
        ),
    ],
)
def test_same_web_url_is_independent_in_other_guilds_and_dm_rooms(
    memory_factory, monkeypatch, sources
):
    async def run():
        memory = memory_factory()
        monkeypatch.setattr(
            memory,
            "_embed",
            AsyncMock(return_value=np.ones(EMBED_DIM, dtype=np.float32)),
        )
        result = [
            {"href": "https://example.com/fact", "title": "Fact", "body": "source text"}
        ]
        for source in sources:
            assert (
                await memory.store_web_results(
                    "query",
                    result,
                    requester=source,
                    scope_to_guild=True,
                )
                == 1
            )
            assert (
                await memory.store_web_results(
                    "query",
                    result,
                    requester=source,
                    scope_to_guild=True,
                )
                == 0
            )
        reopened = memory_factory()
        rows = reopened._db.execute(
            "SELECT id, content_hash FROM vectors WHERE kind='web_result'"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] != rows[1]["id"]
        assert rows[0]["content_hash"] != rows[1]["content_hash"]
        monkeypatch.setattr(
            reopened,
            "_embed",
            AsyncMock(return_value=np.ones(EMBED_DIM, dtype=np.float32)),
        )
        assert (
            await reopened.store_web_results(
                "query", result, requester=sources[0], scope_to_guild=True
            )
            == 0
        )

    asyncio.run(run())


def test_retention_cannot_prune_a_different_guild(memory_factory):
    async def run():
        memory = memory_factory(max_messages=1)
        for guild, message_id in (
            ("one", "first"),
            ("two", "second"),
            ("two", "newest"),
        ):
            await memory.add_to_channel_memory(
                "room",
                {
                    "message_id": message_id,
                    "guild_id": guild,
                    "content": "hello",
                },
            )
        assert [
            row["message_id"]
            for row in await memory.get_channel_memory(
                "room", requester=requester(guild="one")
            )
        ] == ["first"]
        assert [
            row["message_id"]
            for row in await memory.get_channel_memory(
                "room", requester=requester(guild="two")
            )
        ] == ["newest"]

    asyncio.run(run())


def test_metadata_patch_is_sanitized_and_cannot_rewrite_transcript_identity(
    memory_factory,
):
    async def run():
        memory = memory_factory()
        await memory.add_to_channel_memory(
            "room",
            {
                "message_id": "message",
                "guild_id": "guild",
                "author_id": "alice",
                "content": "original",
            },
        )
        patch = {
            "tool_result": "text __IMAGE_B64__QUFB__END_IMAGE_B64__",
            "media": [{"b64": "QUFB"}],
            "content": "forged",
            "message_id": "forged",
        }
        assert await memory.merge_message_metadata("message", patch)
        assert patch["media"][0]["b64"] == "QUFB"
        metadata = memory._db.execute(
            "SELECT metadata FROM vectors WHERE id='message'"
        ).fetchone()[0]
        assert "QUFB" not in metadata
        history = await memory.get_channel_memory("room", requester=requester())
        assert history[0]["content"] == "original"
        assert history[0]["message_id"] == "message"
        fact = await memory.add_long_term_memory("private fact", requester=requester())
        assert not await memory.merge_message_metadata(fact, {"source_user_id": "bob"})

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["ltm", "entity", "shared_context"])
def test_fact_writers_strip_binary_payloads_before_clipping(memory_factory, kind):
    async def run():
        memory = memory_factory()
        content = "description __IMAGE_B64__" + "QUFB" * 1000 + "__END_IMAGE_B64__ tail"
        if kind == "ltm":
            await memory.add_long_term_memory(content, requester=requester())
        elif kind == "entity":
            await memory.add_entity_fact("alice", content, requester=requester())
        else:
            await memory.add_shared_context({"content": content}, requester=requester())
        text = memory._db.execute(
            "SELECT content FROM vectors WHERE kind=?", (kind,)
        ).fetchone()[0]
        assert text == "description [image payload omitted] tail"

    asyncio.run(run())


def _insert_search_vector(memory, name, vector):
    memory._db.execute(
        "INSERT INTO vectors (id, kind, channel_id, guild_id, content, embedding, "
        "timestamp, created_at) VALUES (?, 'message', 'room', 'guild', ?, ?, '', 1)",
        (name, name, _embedding_to_blob(np.asarray(vector, dtype=np.float32))),
    )


@pytest.mark.parametrize("read_only", [False, True])
def test_semantic_query_never_mutates_its_cached_vector(
    memory_factory, monkeypatch, read_only
):
    async def run():
        memory = memory_factory()
        vector = np.full(EMBED_DIM, 2.0, dtype=np.float32)
        before = vector.copy()
        if read_only:
            vector.setflags(write=False)
        monkeypatch.setattr(memory, "_embed_for_query", AsyncMock(return_value=vector))
        _insert_search_vector(memory, "valid", before)
        result = await memory.rag_search("query", requester=requester())
        assert [row["id"] for row in result] == ["valid"]
        np.testing.assert_array_equal(vector, before)

    asyncio.run(run())


@pytest.mark.parametrize("invalid", [np.nan, np.inf, 0.0])
def test_nonfinite_and_zero_vectors_cannot_enter_search_results(
    memory_factory, monkeypatch, invalid
):
    async def run():
        memory = memory_factory()
        good = np.ones(EMBED_DIM, dtype=np.float32)
        _insert_search_vector(memory, "bad", np.full(EMBED_DIM, invalid))
        _insert_search_vector(memory, "good", good)
        monkeypatch.setattr(memory, "_embed_for_query", AsyncMock(return_value=good))
        assert [
            row["id"] for row in await memory.rag_search("query", requester=requester())
        ] == ["good"]
        monkeypatch.setattr(
            memory,
            "_embed_for_query",
            AsyncMock(return_value=np.full(EMBED_DIM, invalid)),
        )
        assert await memory.rag_search("query", requester=requester()) == []

    asyncio.run(run())


def test_openai_batch_embeddings_follow_input_indices_not_response_order():
    assert extract_embeddings(
        {
            "data": [
                {"index": 1, "embedding": [0, 1]},
                {"index": 0, "embedding": [1, 0]},
            ]
        }
    ) == [[1, 0], [0, 1]]


@pytest.mark.parametrize("indices", [[0, 0], [0, 2], [1, None], [0, True]])
def test_ambiguous_openai_batch_indices_fail_closed(indices):
    assert (
        extract_embeddings(
            {"data": [{"index": index, "embedding": [1, 0]} for index in indices]}
        )
        == []
    )


def test_recovery_assigns_reordered_openai_vectors_to_the_correct_rows(
    memory_factory, monkeypatch
):
    async def run():
        memory = memory_factory()
        monkeypatch.setattr(rag_memory, "EMBED_DIM", 2)
        for index, content in enumerate(("first", "second")):
            memory._db.execute(
                "INSERT INTO vectors (id, kind, content, timestamp, created_at) "
                "VALUES (?, 'ltm', ?, '', 1)",
                (str(index), content),
            )

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {
                    "data": [
                        {"index": 1, "embedding": [0, 2]},
                        {"index": 0, "embedding": [2, 0]},
                    ]
                }

        class Session:
            def post(self, _url, *, json, **_kwargs):
                assert json["input"] == ["first", "second"]
                return Response()

        monkeypatch.setattr(
            memory, "_get_embed_session", AsyncMock(return_value=Session())
        )
        await memory._embed_pending_all(batch_size=2)
        rows = memory._db.execute(
            "SELECT embedding FROM vectors ORDER BY id"
        ).fetchall()
        np.testing.assert_array_equal(_blob_to_embedding(rows[0]["embedding"]), [1, 0])
        np.testing.assert_array_equal(_blob_to_embedding(rows[1]["embedding"]), [0, 1])
        assert memory._active_embed_row_ids == set()

    asyncio.run(run())


def test_edit_during_embedding_keeps_new_text_pending_for_recovery(
    memory_factory, monkeypatch
):
    async def run():
        memory = memory_factory()
        row_id = await memory.add_long_term_memory("old fact", requester=requester())
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_embed(_text, *, background=False):
            entered.set()
            await release.wait()
            return np.ones(EMBED_DIM, dtype=np.float32)

        monkeypatch.setattr(memory, "_embed", blocked_embed)
        task = asyncio.create_task(memory._embed_and_store(row_id, "old fact"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert await memory.edit_long_term_memory(row_id, "new fact")
        release.set()
        assert not await task
        row = memory._db.execute(
            "SELECT content, embedding FROM vectors WHERE id=?", (row_id,)
        ).fetchone()
        assert row["content"] == "new fact" and row["embedding"] is None
        assert memory._active_embed_row_ids == set()
        assert await memory._embed_and_store(row_id, "new fact")
        assert memory._db.execute(
            "SELECT embedding FROM vectors WHERE id=?", (row_id,)
        ).fetchone()[0]

    asyncio.run(run())


def test_rem_consumers_cannot_mutate_nested_events_and_bad_mentions_are_ignored(
    tmp_path,
):
    async def run():
        log = RemEventLog(str(tmp_path))
        await log.record(
            {
                "role": "user",
                "content": "first",
                "mentions": [{"id": "alice", "name": "Alice"}],
            }
        )
        await log.record({"role": "user", "content": "second", "mentions": 42})
        snapshot = await log.drain_slice()
        snapshot[0]["mentions"][0]["name"] = "mutated"
        assert (await log.drain_slice())[0]["mentions"][0]["name"] == "Alice"
        await log.flush()
        loaded = RemEventLog(str(tmp_path))
        loaded.load_from_disk()
        assert [row["content"] for row in await loaded.drain_slice()] == [
            "first",
            "second",
        ]

    asyncio.run(run())


def test_rem_failed_save_remains_dirty_and_can_be_retried(tmp_path, monkeypatch):
    async def run():
        log = RemEventLog(str(tmp_path))
        await log.record({"role": "user", "content": "keep this event"})
        original = log._atomic_save
        monkeypatch.setattr(
            log, "_atomic_save", AsyncMock(side_effect=OSError("disk unavailable"))
        )
        with pytest.raises(OSError, match="disk unavailable"):
            await log.flush()
        assert log._dirty
        monkeypatch.setattr(log, "_atomic_save", original)
        await log.flush()
        assert not log._dirty
        loaded = RemEventLog(str(tmp_path))
        loaded.load_from_disk()
        assert (await loaded.drain_slice())[0]["content"] == "keep this event"

    asyncio.run(run())
