"""Consumer-visible regressions for privacy deletion, attribution and graph scopes."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from conversation_memory import ConversationMemoryManager
from knowledge_graph import KnowledgeGraph
from maxwell_core.memory.scope import MemoryRequester
from privacy_data import cleanup_data, delete_data, delete_live_data, deletion_text


def _row(mid, author_id, content, second=0, **metadata):
    return {
        "message_id": mid,
        "author_id": author_id,
        "guild_id": "g",
        "content": content,
        "timestamp": f"2026-11-09T00:00:{second:02d}+00:00",
        "privacy_started_at": 1,
        **metadata,
    }


def test_opt_out_keeps_an_interleaved_tool_owned_by_another_turn(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200"):
            memory.preferences.set_default(uid, "tracking", "on")
        requester = MemoryRequester("200", "c", "g")
        try:
            await memory.add_to_channel_memory("c", _row("u100", "100", "private", 0))
            # This worker completes while 100 is the most recent human speaker.
            await memory.add_to_channel_memory("c", _row(
                "tool200", "", "other worker result", 1,
                is_tool=True, requested_by="200", origin_request_id="u200",
            ))
            memory.preferences.set_default("100", "tracking", "off")
            result = await memory.delete_author_content("100")
            assert result["messages"] == 1
            assert [row["message_id"] for row in await memory.get_channel_memory(
                "c", requester=requester,
            )] == ["tool200"]
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_deletion_recognizes_legacy_bot_output_without_metadata_flag(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        try:
            memory._db.execute(
                "INSERT INTO vectors(id,kind,channel_id,guild_id,author_id,content,metadata,timestamp,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("legacy-bot", "bot_output", "c", "g", "999", "private echo",
                 json.dumps({"reply_to_author_id": "100"}), "2026-11-09", time.time()),
            )
            result = await memory.delete_author_content("100")
            assert result["messages"] == 1
            assert memory._db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_queued_quote_cannot_restore_deleted_content_after_reenable(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200"):
            memory.preferences.set_default(uid, "tracking", "on")
        requester = MemoryRequester("200", "c", "g")
        try:
            quoted = _row(
                "other", "200", "quartz question", reply_to_author_id="100",
                reply_to_content="PRIVATE_QUOTE",
            )
            await memory.add_to_channel_memory("c", quoted)
            result = await asyncio.to_thread(delete_data, tmp_path, "100", memory.preferences)
            assert not result["failed"]
            memory.preferences.set_default("100", "tracking", "on")
            # A human reply captured before deletion finishes writing afterwards.
            await memory.add_to_channel_memory("c", dict(quoted, message_id="queued"))
            raw = [json.loads(row[0]) for row in memory._db.execute("SELECT metadata FROM vectors")]
            assert all("reply_to_content" not in row for row in raw)
            history = await memory.get_channel_memory("c", requester=requester)
            recalled = await memory.get_relevant_channel_memory("c", "quartz", requester=requester)
            assert {row["message_id"] for row in history} == {"other", "queued"}
            assert {row["message_id"] for row in recalled} == {"other", "queued"}
            assert all("reply_to_content" not in row for row in history + recalled)
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_readers_scrub_quotes_while_opt_out_deletion_is_pending(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200"):
            memory.preferences.set_default(uid, "tracking", "on")
        try:
            await memory.add_to_channel_memory("c", _row(
                "other", "200", "quartz question", reply_to_author_id="100",
                reply_to_content="PRIVATE_QUOTE",
            ))
            memory.preferences.set_default("100", "tracking", "off")
            # Suppression is already durable, but SQL deletion has not run yet.
            requester = MemoryRequester("200", "c", "g")
            history = await memory.get_channel_memory("c", requester=requester)
            recalled = await memory.get_relevant_channel_memory("c", "quartz", requester=requester)
            assert len(history) == len(recalled) == 1
            assert all("reply_to_content" not in row for row in history + recalled)
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_periodic_cleanup_removes_legacy_unattributed_tool_from_opted_out_turn(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        memory.preferences.set_default("100", "tracking", "off")
        memory.preferences.set_default("200", "tracking", "on")
        try:
            for row in (
                _row("user", "100", "PRIVATE_TURN", 0),
                _row("legacy-tool", "", "PRIVATE_TOOL", 1, is_tool=True),
                _row("other", "200", "keep", 2),
            ):
                memory._db.execute(
                    "INSERT INTO vectors(id,kind,channel_id,guild_id,author_id,content,metadata,timestamp,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["message_id"], "message", "c", "g", row["author_id"], row["content"],
                     json.dumps({"is_tool": row.get("is_tool", False)}), row["timestamp"], time.time()),
                )
            await asyncio.to_thread(cleanup_data, tmp_path, memory.preferences)
            assert [row[0] for row in memory._db.execute("SELECT id FROM vectors")] == ["other"]
            rows = await memory.get_channel_memory("c", requester=MemoryRequester("200", "c", "g"))
            assert [row["content"] for row in rows] == ["keep"]
        finally:
            await memory.flush()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_scope_column", [False, True])
def test_graph_edge_migration_keeps_same_public_nodes_in_independent_scopes(tmp_path, legacy_scope_column):
    db = sqlite3.connect(tmp_path / "graph.db")
    db.row_factory = sqlite3.Row
    try:
        scope_column = ", scope TEXT NOT NULL DEFAULT ''" if legacy_scope_column else ""
        db.execute(
            "CREATE TABLE graph_edges(src TEXT, rel TEXT, dst TEXT, props TEXT, updated_at REAL"
            + scope_column + ", PRIMARY KEY(src,rel,dst))"
        )
        db.execute("INSERT INTO graph_edges(src,rel,dst,props,updated_at) VALUES ('old','USES','legacy','{}',0)")
        db.commit()
        graph = KnowledgeGraph(db)
        graph.index_site(slug="alpha", title="Alpha")
        graph.index_site(slug="bravo", title="Bravo")
        sources = [MemoryRequester(uid, channel, "g", is_admin=True) for uid, channel in (
            ("100", "private100"), ("200", "private200"),
        )]
        for source in sources:
            assert graph.ingest_triples(
                [{"s": "alpha", "rel": "USES", "o": "bravo"}], requester=source,
            ) == 1
        # Previously the second ingestion silently moved the first scope's edge.
        for source in sources:
            assert "Alpha --USES--> Bravo" in graph.prompt_block(query="alpha", requester=source)
        unrelated = MemoryRequester("300", "unrelated", "g", is_admin=True)
        assert graph.prompt_block(query="alpha", requester=unrelated) == ""
        assert db.execute("SELECT count(*) FROM graph_edges WHERE src='old' AND scope=''").fetchone()[0] == 1
        db.commit()
        # Reopening the new schema must neither migrate again nor lose either fact.
        restored = KnowledgeGraph(db)
        for source in sources:
            assert "USES" in restored.prompt_block(query="alpha", requester=source)
    finally:
        db.close()


def test_full_deletion_removes_attributed_graph_facts_and_private_labels(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        memory.preferences.set_default("100", "tracking", "on")
        memory.preferences.set_default("200", "tracking", "on")
        graph = KnowledgeGraph(memory._db)
        source = MemoryRequester("100", "private100", is_dm=True, is_admin=True)
        other = MemoryRequester("200", "private200", is_dm=True, is_admin=True)
        try:
            graph.index_site(slug="keepsite", title="PublicSite", owner_id="100")
            graph.upsert_node(
                "chat-unrelated:user:200", "user", "OtherIdentity", {"user_id": "200"}
            )
            for requester, label in ((source, "PrivateDiagnosis"), (other, "OtherProject")):
                graph.ingest_triples(
                    [{"s": "project", "rel": "USES", "o": label}], requester=requester,
                )
            result = await asyncio.to_thread(delete_data, tmp_path, "100", memory.preferences)
            assert not result["failed"]
            assert result["deleted"]["conversation"]["graph"] == 2
            assert graph.prompt_block(query="PrivateDiagnosis", requester=source) == ""
            assert not memory._db.execute("SELECT 1 FROM graph_nodes WHERE name='PrivateDiagnosis'").fetchone()
            assert "OtherProject" in graph.prompt_block(query="OtherProject", requester=other)
            assert memory._db.execute("SELECT 1 FROM graph_nodes WHERE id='site:keepsite'").fetchone()
            assert not memory._db.execute("SELECT 1 FROM graph_nodes WHERE id='user:100'").fetchone()
            assert memory._db.execute(
                "SELECT 1 FROM graph_nodes WHERE id='chat-unrelated:user:200'"
            ).fetchone()
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_live_deletion_clears_hydrated_quotes_and_reports_failed_shell_recycle(tmp_path):
    class Shell:
        @classmethod
        async def discard_owner(cls, uid):
            raise RuntimeError("PRIVATE_EXCEPTION")

    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        bot = SimpleNamespace(
            _user_preferences=memory.preferences,
            config=SimpleNamespace(DATA_DIR=tmp_path),
            tools={"shell": Shell()},
            _hydration={"message": {"reply_to_content": "PRIVATE_QUOTE"}},
        )
        try:
            result = await delete_live_data(bot, "100")
            assert bot._hydration == {}
            assert "shell_sandboxes" in result["failed"]
            assert "PRIVATE_EXCEPTION" not in deletion_text(result)
            assert not memory.preferences.content_allowed("100")
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_opt_out_strips_reply_and_mention_identity(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200"):
            memory.preferences.set_default(uid, "tracking", "on")
        requester = MemoryRequester("200", "c", "g")
        try:
            await memory.add_to_channel_memory("c", _row(
                "other", "200", "quartz question",
                reply_to_author="AliceSecret",
                reply_to_author_id="100",
                reply_to_content="PRIVATE_QUOTE",
                mentions=[
                    {"id": "100", "name": "AliceSecret"},
                    {"id": "200", "name": "Bob"},
                ],
            ))
            memory.preferences.set_default("100", "tracking", "off")
            history = await memory.get_channel_memory("c", requester=requester)
            assert "AliceSecret" not in json.dumps(history)
            assert "reply_to_author" not in history[0]
            assert history[0]["mentions"] == [{"id": "200", "name": "Bob"}]
            await memory.delete_author_content("100")
            raw = json.loads(memory._db.execute("SELECT metadata FROM vectors").fetchone()[0])
            assert "AliceSecret" not in json.dumps(raw)
            assert raw["mentions"] == [{"id": "200", "name": "Bob"}]
            memory.preferences.set_default("100", "tracking", "on")
            queued = _row(
                "queued", "200", "later",
                reply_to_author="AliceSecret",
                reply_to_author_id="100",
                reply_to_content="PRIVATE_QUOTE",
                mentions=[{"id": "100", "name": "AliceSecret"}],
            )
            await memory.add_to_channel_memory("c", queued)
            stored = [
                json.loads(row[0])
                for row in memory._db.execute("SELECT metadata FROM vectors")
            ]
            assert all("AliceSecret" not in json.dumps(row) for row in stored)
            restored = await memory.get_channel_memory("c", requester=requester)
            assert "AliceSecret" not in json.dumps(restored)
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_author_deletion_removes_detached_chat_labels(tmp_path):
    from knowledge_graph import delete_author_graph

    db = sqlite3.connect(tmp_path / "graph.db")
    db.row_factory = sqlite3.Row
    try:
        graph = KnowledgeGraph(db)
        source = MemoryRequester("100", "private100", is_dm=True, is_admin=True)
        other = MemoryRequester("200", "private200", is_dm=True, is_admin=True)
        graph.index_site(slug="keepsite", title="PublicSite")
        graph.index_site(slug="staysite", title="Stay")
        assert graph.ingest_triples(
            [{"s": "PrivateDiagnosis", "rel": "USES", "o": "PrivateDiagnosis"}],
            requester=source,
        ) == 0
        assert graph.ingest_triples(
            [{"s": "keepsite", "rel": "USES", "o": "OrphanLabel"}],
            requester=source,
        ) == 1
        graph.ingest_triples(
            [{"s": "project", "rel": "USES", "o": "OtherProject"}],
            requester=other,
        )
        graph.upsert_node(
            "chat-legacy:thing:oldsecret", "thing", "OldSecret",
            {"source_scope": "dm:100:private100"},
        )
        graph.upsert_node(
            "chat-guild:thing:guildlabel", "thing", "GuildLabel",
            {"source_scope": "channel:g:room"},
        )
        graph.drop_site("keepsite")
        delete_author_graph(db, "100")
        names = {row[0] for row in db.execute("SELECT name FROM graph_nodes")}
        assert "PrivateDiagnosis" not in names
        assert "OrphanLabel" not in names
        assert "OldSecret" not in names
        assert "OtherProject" in names
        assert "GuildLabel" in names
        assert db.execute("SELECT 1 FROM graph_nodes WHERE id='site:staysite'").fetchone()
    finally:
        db.close()


def test_live_deletion_drops_author_from_watch_queue_and_journal(tmp_path):
    from message_pipeline import ReplyQueue, RequestJournal, _ChannelState, _Pending

    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        journal = RequestJournal(tmp_path / "requests.sqlite3")
        try:
            journal.accept("10", "22", "100", directed=True)
            journal.update("10", "deferred", reason="cancelled")
            journal.accept("11", "22", "200", directed=True)
            journal.update("11", "queued")
            deleted = SimpleNamespace(
                id="10", author=SimpleNamespace(id="100"), content="PRIVATE_TURN",
            )
            kept = SimpleNamespace(id="12", author=SimpleNamespace(id="200"))
            other = SimpleNamespace(
                id="11", author=SimpleNamespace(id="200"), content="keep",
                _watch_burst=[deleted, kept],
            )
            queue = ReplyQueue()
            state = _ChannelState()
            state.queue = [
                _Pending(deleted, "PRIVATE_TURN", True, 0.0),
                _Pending(other, "keep", True, 0.0, burst=[deleted, kept]),
            ]
            queue._channels["22"] = state
            queue._outstanding = 2
            unrelated = SimpleNamespace(id="30", author=SimpleNamespace(id="300"))

            async def linger():
                await asyncio.sleep(30)

            task = asyncio.create_task(linger())
            bot = SimpleNamespace(
                _user_preferences=memory.preferences,
                config=SimpleNamespace(DATA_DIR=tmp_path),
                _request_journal=journal,
                _reply_queue=queue,
                _watch_debounce={
                    "22": {
                        "latest": deleted,
                        "burst": [deleted],
                        "task": task,
                        "content": "PRIVATE_TURN",
                    },
                    "33": {"latest": unrelated, "burst": [unrelated], "task": None},
                },
            )
            await delete_live_data(bot, "100")
            assert task.done()
            assert "22" not in bot._watch_debounce
            assert "33" in bot._watch_debounce
            assert [entry.message_id for entry in queue._channels["22"].queue] == ["11"]
            assert queue._channels["22"].queue[0].burst == [kept]
            assert [item.id for item in other._watch_burst] == ["12"]
            assert journal.get("10")["status"] == "suppressed"
            assert journal.get("10")["reason"] == "privacy_deletion"
            assert journal.get("11")["status"] == "queued"
        finally:
            journal.close()
            await memory.flush()

    asyncio.run(scenario())

