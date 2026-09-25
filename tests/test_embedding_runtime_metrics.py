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
