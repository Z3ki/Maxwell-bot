import asyncio

import numpy as np

import rag_memory
from rag_memory import EMBED_DIM, RAGMemoryManager


def test_embedding_client_reuses_and_closes_session(tmp_path):
    async def run():
        manager = RAGMemoryManager(str(tmp_path))
        session = await manager._get_embed_session()
        assert await manager._get_embed_session() is session
        await manager.flush()
        assert session.closed
        manager._db.close()

    asyncio.run(run())


def test_embedding_metrics_count_success_failure_and_cache_work(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(rag_memory, "EMBEDDINGS_ENABLED", True)
        manager = RAGMemoryManager(str(tmp_path))
        vector = np.ones(EMBED_DIM, dtype=np.float32)

        async def succeeds(_text, *, background=False):
            return vector

        monkeypatch.setattr(manager, "_embed_impl", succeeds)
        assert await manager._embed("first query") is vector

        async def fails(_text, *, background=False):
            return None

        monkeypatch.setattr(manager, "_embed_impl", fails)
        assert await manager._embed("second query") is None

        metrics = manager.get_embedding_metrics()
        assert metrics["requests"] == 2
        assert metrics["failures"] == 1
        assert metrics["background_queue_depth"] == 0
        assert metrics["pending_work"] == 0
        assert metrics["average_processing_ms"] >= 0
        await manager.flush()
        manager._db.close()

    asyncio.run(run())



def test_embedding_recovery_worker_retries_pending_rows_and_is_singleton(
    tmp_path, monkeypatch
):
    async def run():
        monkeypatch.setattr(rag_memory, "EMBEDDINGS_ENABLED", True)
        manager = RAGMemoryManager(str(tmp_path))
        manager._db.execute(
            "INSERT INTO vectors (id, kind, content, embedding, timestamp, created_at) "
            "VALUES ('pending', 'ltm', 'recover me', NULL, '', 0)"
        )
        monkeypatch.setattr(rag_memory, "EMBED_RECOVERY_INTERVAL_SECONDS", 0.01)
        paused = {"value": True}
        monkeypatch.setattr(
            manager, "_embed_endpoint_paused", lambda: paused["value"]
        )
        embedded = asyncio.Event()

        class _Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                embedded.set()
                return False

            async def json(self):
                return {"embeddings": [np.ones(EMBED_DIM, dtype=np.float32).tolist()]}

        class _Session:
            def __init__(self):
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return _Response()

        session = _Session()

        async def _get_session():
            return session

        monkeypatch.setattr(manager, "_get_embed_session", _get_session)
        worker = manager.start_embedding_recovery_worker()
        assert manager.start_embedding_recovery_worker() is worker
        await asyncio.sleep(0.03)
        assert session.calls == 0

        # Simulate the embedding service becoming available after an outage.
        paused["value"] = False
        await asyncio.wait_for(embedded.wait(), timeout=1)
        row = manager._db.execute(
            "SELECT embedding FROM vectors WHERE id='pending'"
        ).fetchone()
        assert row["embedding"] is not None
        assert session.calls == 1
        metrics = manager.get_embedding_metrics()
        assert metrics["pending_work"] == 0
        assert metrics["recovery_worker_running"]

        await manager.flush()
        assert worker.done()
        assert not manager.get_embedding_metrics()["recovery_worker_running"]
        manager._db.close()

    asyncio.run(run())


def test_embedding_recovery_worker_cancellation_preserves_pending_text(
    tmp_path, monkeypatch
):
    async def run():
        monkeypatch.setattr(rag_memory, "EMBEDDINGS_ENABLED", True)
        manager = RAGMemoryManager(str(tmp_path))
        manager._db.execute(
            "INSERT INTO vectors (id, kind, content, embedding, timestamp, created_at) "
            "VALUES ('pending', 'ltm', 'recover me', NULL, '', 0)"
        )
        monkeypatch.setattr(rag_memory, "EMBED_RECOVERY_INTERVAL_SECONDS", 0.01)
        entered_embed = asyncio.Event()
        wait_forever = asyncio.Event()

        class _Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                entered_embed.set()
                await wait_forever.wait()
                return {"embeddings": [np.ones(EMBED_DIM, dtype=np.float32).tolist()]}

        class _Session:
            closed = False

            def post(self, *_args, **_kwargs):
                return _Response()

            async def close(self):
                self.closed = True

        session = _Session()
        manager._embed_session = session
        manager._embed_session_loop = asyncio.get_running_loop()

        async def _get_session():
            return session

        monkeypatch.setattr(manager, "_get_embed_session", _get_session)
        worker = manager.start_embedding_recovery_worker()
        await asyncio.wait_for(entered_embed.wait(), timeout=1)
        assert manager._active_embed_row_ids == {"pending"}

        await manager.flush()
        assert worker.done()
        assert session.closed
        assert manager._active_embed_row_ids == set()
        row = manager._db.execute(
            "SELECT content, embedding FROM vectors WHERE id='pending'"
        ).fetchone()
        assert row["content"] == "recover me"
        assert row["embedding"] is None
        manager._db.close()

    asyncio.run(run())


def test_pending_embedding_index_supports_ordered_backlog_scan(tmp_path):
    manager = RAGMemoryManager(str(tmp_path))
    plan = manager._db.execute(
        "EXPLAIN QUERY PLAN SELECT rowid, id, content FROM vectors "
        "WHERE embedding IS NULL AND rowid > 0 ORDER BY rowid LIMIT 4"
    ).fetchall()
    assert any("idx_vectors_pending_embedding" in row[3] for row in plan)
    manager._db.close()
