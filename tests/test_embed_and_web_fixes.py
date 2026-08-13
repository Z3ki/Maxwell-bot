"""Regression tests for the 2026-08-10 embed/web_result bug sweep.

Each test here pins a bug that was live in the tree at 9b6755c:

  1. embed_cache seed used a [:8000] key while _embed() had been changed
     to hash the FULL stripped text, so every seeded entry for long
     content was dead weight and re-embedded on every restart.
  2. _embed_pending_all() truncated batch inputs at 8000 chars even
     though _embed() supports EMBED_MAX_CHARS with chunking, so the
     migration path permanently stored truncated vectors for long rows.
  3. _embed_pending_all() re-SELECTed `embedding IS NULL` forever when
     embedding kept failing — an unbounded spin.
  4. store_web_results() stored the title-weighted embed text as the row
     `content`, so the prompt rendered the title three times and spent
     the snippet budget on repetition instead of the body.
  5. store_web_results(ttl_days=...) was accepted, documented, and then
     silently ignored by the pruner.
"""

import asyncio

import numpy as np

import rag_memory
from bot import _web_result_snippet
from rag_memory import (
    EMBED_DIM,
    EMBED_MAX_CHARS,
    LEGACY_EMBED_TRUNCATE,
    RAGMemoryManager,
    WEB_RESULT_KIND,
    _split_embed_chunks,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _unit_vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-8)


# ─── 1. embed_cache seeding ──────────────────────────────────────────


def test_embed_cache_seed_skips_legacy_truncated_rows(tmp_path, monkeypatch):
    """Rows over the old 8000-char cutoff must not be seeded into the cache.

    Their stored vector came from truncated text, so caching it under the
    full-text key would hand _embed() a vector that does not correspond
    to the text it was asked to embed.
    """
    mgr = RAGMemoryManager(str(tmp_path))
    short = "s" * 100
    long = "L" * (LEGACY_EMBED_TRUNCATE + 5000)
    for i, content in enumerate((short, long)):
        mgr._db.execute(
            "INSERT INTO vectors (id, kind, content, embedding, created_at, "
            " updated_at, timestamp, channel_id, guild_id, author, author_id, "
            " source, content_hash, metadata, scope, importance, parent_id, "
            " chunk_index, downvotes) "
            "VALUES (?, 'ltm', ?, ?, 0, 0, '', '', '', '', '', '', '', '{}', "
            " '', 0, '', 0, 0)",
            (f"row{i}", content, rag_memory._embedding_to_blob(_unit_vec(i))),
        )

    # Re-open: __init__ runs the embed_cache seed over existing rows.
    mgr2 = RAGMemoryManager(str(tmp_path))
    import hashlib

    short_key = hashlib.sha256(short.encode()).hexdigest()
    long_key = hashlib.sha256(long.encode()).hexdigest()

    def _has(key):
        return (
            mgr2._db.execute(
                "SELECT COUNT(*) AS c FROM embed_cache WHERE key=?", (key,)
            ).fetchone()["c"]
            > 0
        )

    assert _has(short_key), "short row should seed the cache under its full-text key"
    assert not _has(long_key), "legacy-truncated long row must NOT be seeded"
    # The long row must contribute NO cache entry at all. Checking only the
    # full-text key would pass even with the old [:8000] seed, which wrote a
    # never-matched key rather than writing nothing.
    total = mgr2._db.execute("SELECT COUNT(*) AS c FROM embed_cache").fetchone()["c"]
    assert total == 1, (
        f"expected only the short row cached, got {total} entries — "
        "the long row was seeded under an unreachable key"
    )


def test_embed_cache_seed_key_matches_embed_lookup(tmp_path, monkeypatch):
    """A seeded short row must actually produce a cache HIT in _embed().

    This is the bug that made the seed dead weight: the seed wrote one key
    and _embed() looked up a different one, so the embed API was hit anyway.
    """
    content = "the quick brown fox jumps over the lazy dog"
    vec = _unit_vec(7)
    mgr = RAGMemoryManager(str(tmp_path))
    mgr._db.execute(
        "INSERT INTO vectors (id, kind, content, embedding, created_at, "
        " updated_at, timestamp, channel_id, guild_id, author, author_id, "
        " source, content_hash, metadata, scope, importance, parent_id, "
        " chunk_index, downvotes) "
        "VALUES ('r', 'ltm', ?, ?, 0, 0, '', '', '', '', '', '', '', '{}', "
        " '', 0, '', 0, 0)",
        (content, rag_memory._embedding_to_blob(vec)),
    )

    mgr2 = RAGMemoryManager(str(tmp_path))

    # Any HTTP attempt means the cache missed.
    def _boom(*a, **k):
        raise AssertionError("embed API called — cache seed did not match lookup")

    monkeypatch.setattr(rag_memory.aiohttp, "ClientSession", _boom)

    got = _run(mgr2._embed(content))
    assert got is not None
    assert np.allclose(got, vec, atol=1e-6)


# ─── 2/3. batch migration path ───────────────────────────────────────


def test_split_embed_chunks_respects_cap():
    assert _split_embed_chunks("short") == ["short"]
    big = ("sentence here. " * 6000)[: EMBED_MAX_CHARS * 2 + 500]
    chunks = _split_embed_chunks(big)
    assert len(chunks) > 1
    assert all(len(c) <= EMBED_MAX_CHARS for c in chunks)


def test_embed_pending_all_does_not_truncate_long_rows(tmp_path, monkeypatch):
    """Long rows must go through the chunking path, not a silent [:8000]."""
    mgr = RAGMemoryManager(str(tmp_path))
    long_content = "x" * (EMBED_MAX_CHARS + 1000)
    mgr._db.execute(
        "INSERT INTO vectors (id, kind, content, embedding, created_at, "
        " updated_at, timestamp, channel_id, guild_id, author, author_id, "
        " source, content_hash, metadata, scope, importance, parent_id, "
        " chunk_index, downvotes) "
        "VALUES ('long', 'ltm', ?, NULL, 0, 0, '', '', '', '', '', '', '', "
        " '{}', '', 0, '', 0, 0)",
        (long_content,),
    )

    seen: list[str] = []

    async def _embed_stub(self, text):
        seen.append(text)
        return _unit_vec(1)

    monkeypatch.setattr(RAGMemoryManager, "_embed", _embed_stub)
    _run(mgr._embed_pending_all(batch_size=10))

    assert seen, "_embed was never called for the long row"
    # The old bug handed the batch API text[:8000]. The whole row must reach
    # the embedder (as one or more chunks), never a truncated prefix.
    assert sum(len(t) for t in seen) >= len(long_content) - 1000
    assert not any(len(t) == LEGACY_EMBED_TRUNCATE for t in seen)

    row = mgr._db.execute("SELECT embedding FROM vectors WHERE id='long'").fetchone()
    assert row["embedding"] is not None


def test_embed_pending_all_terminates_when_embedding_always_fails(
    tmp_path, monkeypatch
):
    """A permanently failing embedder must not spin the NULL-row SELECT."""
    mgr = RAGMemoryManager(str(tmp_path))
    for i in range(5):
        mgr._db.execute(
            "INSERT INTO vectors (id, kind, content, embedding, created_at, "
            " updated_at, timestamp, channel_id, guild_id, author, author_id, "
            " source, content_hash, metadata, scope, importance, parent_id, "
            " chunk_index, downvotes) "
            "VALUES (?, 'ltm', ?, NULL, 0, 0, '', '', '', '', '', '', '', "
            " '{}', '', 0, '', 0, 0)",
            (f"n{i}", f"content {i}"),
        )

    calls = {"n": 0}

    # _embed_pending_all wraps its whole body in `except Exception`, so an
    # AssertionError raised here would be SWALLOWED and the run would look
    # like a clean exit even while spinning. Signal via BaseException so the
    # runaway actually surfaces.
    class _Runaway(BaseException):
        pass

    async def _embed_fail(self, text):
        calls["n"] += 1
        if calls["n"] > 200:
            raise _Runaway("infinite loop: _embed_pending_all never terminated")
        return None

    monkeypatch.setattr(RAGMemoryManager, "_embed", _embed_fail)

    def _no_http(*a, **k):
        raise RuntimeError("batch API down")

    monkeypatch.setattr(rag_memory.aiohttp, "ClientSession", _no_http)

    try:
        _run(mgr._embed_pending_all(batch_size=2))
    except _Runaway as e:  # pragma: no cover - only on regression
        raise AssertionError(str(e)) from e

    # 5 rows, each attempted exactly once.
    assert calls["n"] == 5, f"expected one attempt per row, got {calls['n']}"

    still_null = mgr._db.execute(
        "SELECT COUNT(*) AS c FROM vectors WHERE embedding IS NULL"
    ).fetchone()["c"]
    assert still_null == 5, "rows should remain NULL, but the loop must still exit"


# ─── 4. stored content vs embed text ─────────────────────────────────


def _stub_simple_embed(monkeypatch):
    async def _embed_stub(self, text):
        return _unit_vec(abs(hash(str(text))) % 10_000)

    monkeypatch.setattr(RAGMemoryManager, "_embed", _embed_stub)


def test_store_web_results_does_not_store_doubled_title(tmp_path, monkeypatch):
    _stub_simple_embed(monkeypatch)
    mgr = RAGMemoryManager(str(tmp_path))
    title = "Python asyncio guide"
    body = "Event loops, coroutines and tasks explained."
    n = _run(
        mgr.store_web_results(
            "asyncio",
            [{"title": title, "href": "https://ex.com/a", "body": body}],
        )
    )
    assert n == 1
    content = mgr._db.execute(
        "SELECT content FROM vectors WHERE kind=?", (WEB_RESULT_KIND,)
    ).fetchone()["content"]
    assert content.count(title) == 1, f"title stored more than once: {content!r}"
    assert body in content


def test_web_result_snippet_strips_repeated_title():
    title = "Python asyncio guide"
    body = "Event loops, coroutines and tasks explained."
    # New-format row (title once).
    assert _web_result_snippet(f"{title}\n{body}", title) == body
    # Legacy row written before the fix (title twice).
    assert _web_result_snippet(f"{title}\n{title}\n{body}", title) == body
    # No title match — content passes through untouched.
    assert _web_result_snippet(body, "unrelated") == body
    # Missing title must not blow up.
    assert _web_result_snippet(f"{title}\n{body}", "") == f"{title}\n{body}"
    assert _web_result_snippet("", title) == ""


def test_web_result_snippet_respects_limit():
    assert len(_web_result_snippet("z" * 900, "t", limit=280)) == 280


# ─── 5. ttl_days is honored ──────────────────────────────────────────


def test_store_web_results_honors_ttl_days_override(tmp_path, monkeypatch):
    """A short per-call ttl_days must actually prune, not be ignored."""
    _stub_simple_embed(monkeypatch)
    mgr = RAGMemoryManager(str(tmp_path))
    _run(
        mgr.store_web_results(
            "old query",
            [{"title": "Old", "href": "https://ex.com/old", "body": "stale"}],
        )
    )
    # Backdate the row by 3 days.
    import time as _t

    mgr._db.execute(
        "UPDATE vectors SET created_at=? WHERE kind=?",
        (_t.time() - 3 * 86400, WEB_RESULT_KIND),
    )

    # Store again with a 1-day TTL — the 3-day-old row must be pruned.
    _run(
        mgr.store_web_results(
            "new query",
            [{"title": "New", "href": "https://ex.com/new", "body": "fresh"}],
            ttl_days=1,
        )
    )
    urls = [
        r["content"]
        for r in mgr._db.execute(
            "SELECT content FROM vectors WHERE kind=?", (WEB_RESULT_KIND,)
        ).fetchall()
    ]
    assert any("fresh" in u for u in urls)
    assert not any("stale" in u for u in urls), "ttl_days override was ignored"


def test_recall_max_age_days_filters_without_deleting(tmp_path, monkeypatch):
    """max_age_days is a per-read filter — it must never prune the store.

    A caller narrowing its own view must not destroy rows that other
    callers (other guilds, other channels) still expect to exist.
    """
    _stub_simple_embed(monkeypatch)
    mgr = RAGMemoryManager(str(tmp_path))
    _run(
        mgr.store_web_results(
            "asyncio tasks",
            [
                {
                    "title": "Asyncio tasks",
                    "href": "https://ex.com/a",
                    "body": "coroutines and tasks",
                }
            ],
        )
    )
    import time as _t

    # 3 days old: inside the 7-day default TTL, outside a 1-day read filter.
    mgr._db.execute(
        "UPDATE vectors SET created_at=? WHERE kind=?",
        (_t.time() - 3 * 86400, WEB_RESULT_KIND),
    )

    _run(mgr.recall_web_results("asyncio tasks", top_k=4, max_age_days=1))

    remaining = mgr._db.execute(
        "SELECT COUNT(*) AS c FROM vectors WHERE kind=?", (WEB_RESULT_KIND,)
    ).fetchone()["c"]
    assert remaining == 1, "a narrow max_age_days read deleted rows from the store"


def test_rag_query_timeout_opens_short_circuit(tmp_path, monkeypatch):
    """A slow embedder must not make the same turn retry three times."""
    mgr = RAGMemoryManager(str(tmp_path))
    calls = {"count": 0}

    async def slow_embed(_text):
        calls["count"] += 1
        await asyncio.sleep(0.05)
        return _unit_vec(42)

    monkeypatch.setattr(mgr, "_embed", slow_embed)
    monkeypatch.setattr(rag_memory, "RAG_QUERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(rag_memory, "RAG_QUERY_FAILURE_COOLDOWN_SECONDS", 5.0)

    first = _run(mgr.rag_search("slow query", kinds=["ltm"]))
    second = _run(mgr.rag_search("slow query", kinds=["message"]))

    assert first == []
    assert second == []
    assert calls["count"] == 1
