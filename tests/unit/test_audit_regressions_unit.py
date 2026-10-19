"""Regression tests for defects found in the full-codebase audit.

Each test pins one concrete bug: the assertion is written so it fails on the
pre-fix code and passes after the fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# tooling.helpers: `$(( ... << ... ))` is arithmetic, not a heredoc
# --------------------------------------------------------------------------
def test_arithmetic_shift_is_not_reported_as_an_unterminated_heredoc():
    from tooling.helpers import _unterminated_heredoc_error

    assert _unterminated_heredoc_error("echo $((1 << 2))\necho done") is None
    assert _unterminated_heredoc_error("x=$((a << b))\necho $x") is None


def test_real_heredocs_are_still_detected_and_validated():
    from tooling.helpers import _unterminated_heredoc_error

    assert _unterminated_heredoc_error("cat << EOF\nhello\nEOF") is None
    err = _unterminated_heredoc_error("cat << EOF\nnever closed")
    assert err is not None and "EOF" in err
    # A digit-delimited heredoc is still flagged as unparseable, as before.
    assert _unterminated_heredoc_error("cat <<2\nx\n2") is not None


# --------------------------------------------------------------------------
# web_page_text: an unclosed skipped element must not swallow the page
# --------------------------------------------------------------------------
def _local_module(rel_path, name):
    """Load a repo module by path.

    Other test modules prepend a sibling checkout to sys.path, so a plain
    import can resolve to a different tree. Load the file under this repo root.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mismatched_skip_close_does_not_drop_the_rest_of_the_page():
    extract_page_text = _local_module(
        "web_page_text.py", "audit_web_page_text"
    ).extract_page_text

    html = "<body><nav><footer>x</nav><p>REAL</p></body>"
    assert "REAL" in extract_page_text(html)


def test_closed_skip_elements_still_remove_boilerplate():
    extract_page_text = _local_module(
        "web_page_text.py", "audit_web_page_text"
    ).extract_page_text

    html = "<body><nav>menu</nav><p>kept</p><script>var x=1;</script></body>"
    text = extract_page_text(html)
    assert "kept" in text and "menu" not in text and "var x" not in text


# --------------------------------------------------------------------------
# message_pipeline.Watermarks: thread-safe snapshot + batched suppression
# --------------------------------------------------------------------------
def test_watermark_save_is_safe_against_concurrent_note(tmp_path):
    from message_pipeline import Watermarks

    marks = Watermarks(str(tmp_path / "watermarks.json"))
    marks.note("1", 10)
    stop = threading.Event()
    errors: list[BaseException] = []

    def churn():
        value = 100
        while not stop.is_set():
            value += 1
            marks.note(str(value % 7), value)

    def saver():
        try:
            for _ in range(200):
                marks.note("999", int(time.monotonic() * 1_000_000) % 10**6 + 1)
                marks.save()
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=churn) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        saver()
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert not errors


def test_journal_suppresses_stale_rows_in_one_pass(tmp_path):
    from message_pipeline import RequestJournal

    journal = RequestJournal(str(tmp_path / "journal.db"))
    try:
        for mid in ("1", "2", "3"):
            journal.accept(mid, "22", "11", directed=True)
        journal.accept("fresh", "22", "11", directed=True)
        cutoff = time.time()
        # Make the first three look like they predate this process, and pin the
        # fresh one clearly after the cutoff.
        with journal._transaction(write=True) as connection:
            connection.execute(
                "UPDATE requests SET created_at = ? WHERE message_id IN ('1','2','3')",
                (cutoff - 60,),
            )
            connection.execute(
                "UPDATE requests SET created_at = ? WHERE message_id = 'fresh'",
                (cutoff + 10,),
            )
        assert journal.suppress_stale(cutoff) == 3
        assert journal.get("1")["status"] == "suppressed"
        assert journal.get("1")["reason"] == "stale_offline"
        assert journal.get("fresh")["status"] == "received"
    finally:
        journal.close()


def test_response_owners_are_pruned_with_their_request(tmp_path):
    from message_pipeline import RequestJournal

    journal = RequestJournal(str(tmp_path / "journal.db"))
    try:
        journal.accept("m1", "22", "11", directed=True)
        journal.note_response("m1", "resp-1")
        assert journal.find_response("resp-1")["message_id"] == "m1"
        journal.update("m1", "delivered")
        with journal._transaction(write=True) as connection:
            connection.execute("DELETE FROM requests WHERE message_id='m1'")
            journal._prune(connection)
        with journal._transaction() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) AS c FROM response_owners"
            ).fetchone()
        assert rows["c"] == 0
    finally:
        journal.close()


# --------------------------------------------------------------------------
# runtime_lifecycle: shard ids must stay inside shard_count
# --------------------------------------------------------------------------
def test_shard_ids_out_of_range_are_dropped(monkeypatch):
    from runtime_lifecycle import shard_launch_kwargs

    monkeypatch.setenv("SHARD_COUNT", "2")
    monkeypatch.setenv("SHARD_IDS", "0,2")
    kwargs = shard_launch_kwargs()
    assert kwargs["shard_count"] == 2
    assert kwargs["shard_ids"] == [0]


def test_shard_count_derived_from_ids_still_works(monkeypatch):
    from runtime_lifecycle import shard_launch_kwargs

    monkeypatch.delenv("SHARD_COUNT", raising=False)
    monkeypatch.setenv("SHARD_IDS", "0,1,2")
    kwargs = shard_launch_kwargs()
    assert kwargs == {"shard_count": 3, "shard_ids": [0, 1, 2]}


# --------------------------------------------------------------------------
# provider usage: cache tokens are kept when prompt_tokens is absent
# --------------------------------------------------------------------------
def test_cache_tokens_survive_a_missing_prompt_count():
    from maxwell_core.providers.usage import _normalize_llm_usage

    out = _normalize_llm_usage({"prompt_tokens_details": {"cached_tokens": 80}})
    assert out.get("cached_tokens") == 80
    # An inconsistent value larger than a known prompt is still rejected.
    out2 = _normalize_llm_usage({"prompt_tokens": 10, "cached_tokens": 80})
    assert "cached_tokens" not in out2


# --------------------------------------------------------------------------
# api.config: an empty MAXWELL_CORS_ORIGIN must not defeat the fallback
# --------------------------------------------------------------------------
def test_empty_cors_origin_falls_back_to_public_base_url():
    env = {
        **os.environ,
        "MAXWELL_CORS_ORIGIN": "",
        "MAXWELL_PUBLIC_BASE_URL": "https://bot.example.com/",
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import api.config as c; print(c.CORS_ORIGIN)"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "https://bot.example.com"


# --------------------------------------------------------------------------
# tooling.helpers: an all-false Permissions object is cached, not missing
# --------------------------------------------------------------------------
def test_all_false_permissions_are_reported_as_none_not_uncached():
    from tooling.helpers import _perms_to_caps

    class _Perms:
        administrator = False

        def __bool__(self):
            return False

    caps, reason = _perms_to_caps(_Perms())
    assert caps == set()
    assert reason == ""
    assert _perms_to_caps(None) == (set(), "permissions are not cached")


# --------------------------------------------------------------------------
# inbox: the overflow line accounts for the rendered slice
# --------------------------------------------------------------------------
def test_inbox_overflow_counts_the_unrendered_slice():
    import asyncio

    from plugins.inbox.impl import InboxListTool

    class _Store:
        async def load_items(self):
            return [{"id": f"note_{i}", "kind": "notice"} for i in range(24)]

        def actionable(self, items):
            return items

        def planner_items(self, items):
            return items  # 24 rows, more than the 20-row slice

        def render_item(self, item, summary_chars=300):
            return item["id"]

    bot = SimpleNamespace(inbox=_Store(), mark_message_tainted=lambda _m: None)
    text = asyncio.run(InboxListTool(bot).execute(SimpleNamespace()))
    assert "… 4 more not shown" in text


def test_inbox_arms_the_taint_gate_for_non_email_rows():
    from plugins.inbox.impl import InboxListTool

    marked: list[object] = []

    class _Store:
        async def load_items(self):
            return [{"id": "friend_1", "kind": "friend_request"}]

        def actionable(self, items):
            return items

        def planner_items(self, items):
            return items

        def render_item(self, item, summary_chars=300):
            return item["id"]

    bot = SimpleNamespace(
        inbox=_Store(), mark_message_tainted=lambda m: marked.append(m)
    )
    tool = InboxListTool(bot)
    message = SimpleNamespace()
    import asyncio

    asyncio.run(tool.execute(message))
    assert marked == [message]


# --------------------------------------------------------------------------
# rag_memory: a long negative is embedded under the same text it stores
# --------------------------------------------------------------------------
def test_long_negative_gets_an_embedding(tmp_path):
    import asyncio

    import numpy as np

    import rag_memory
    from rag_memory import RAGMemoryManager

    async def scenario():
        memory = RAGMemoryManager(str(tmp_path))
        vector = np.ones(rag_memory.EMBED_DIM, dtype=float)
        vector /= float(np.linalg.norm(vector))

        async def fake_embed(_text, background=False):
            return vector

        memory._embed = fake_embed
        try:
            negative_id = await memory.add_negative("x" * 4000, reason="test")
            for task in list(memory._embed_tasks):
                await asyncio.gather(task, return_exceptions=True)
            row = memory._db.execute(
                "SELECT content, embedding FROM vectors WHERE id=?", (negative_id,)
            ).fetchone()
            assert row is not None
            assert row["embedding"] is not None
            assert row["content"] == "x" * 1500
        finally:
            await memory.flush()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# providers.streaming: a truncated custom tool call must not post raw JSON
# --------------------------------------------------------------------------
def _assembler():
    from maxwell_core.providers.streaming import _ChatStreamAssembler, _StreamCallbacks

    return _ChatStreamAssembler(_StreamCallbacks(), custom_tool_calls=True)


def _delta(text):
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def test_truncated_custom_tool_call_is_rejected():
    from maxwell_core.providers.protocol import ProviderProtocolError

    asm = _assembler()
    asm.accept(_delta('{"name": "shell", "arguments": {"command": "ls'))
    with pytest.raises(ProviderProtocolError):
        asm.finish(done=True)


def test_complete_custom_tool_call_still_parses():
    asm = _assembler()
    asm.accept(_delta('{"name": "shell", "arguments": {"command": "ls"}}'))
    result = asm.finish(done=True)
    tools = result["choices"][0]["message"].get("tool_calls")
    assert tools and tools[0]["function"]["name"] == "shell"


def test_ordinary_prose_with_a_stray_brace_is_not_rejected():
    asm = _assembler()
    asm.accept(_delta("here is a brace: { and then ordinary prose"))
    asm.finish(done=True)  # must not raise


# --------------------------------------------------------------------------
# tools.results: a negated "promise" must not force another model turn
# --------------------------------------------------------------------------
def test_negated_promise_is_not_treated_as_a_promise():
    from maxwell_core.tools.results import (
        _only_promise_results,
        _promises_followup_work,
        _tool_results_need_followup,
    )

    refusals = [
        "I'm not going to create that site.",
        "No, I am not going to deploy that.",
    ]
    for text in refusals:
        result = f"Tool send_message: __MESSAGE_SENT__\n{text}"
        assert _promises_followup_work(result) is False, text
        assert _tool_results_need_followup([result]) is False, text
        assert _only_promise_results([result]) is False, text

    # Real promises, including one phrased after a negation, still loop.
    for text in (
        "on it...",
        "not now — I will build it",
        "I won't build it, but I'll test it",
    ):
        assert (
            _promises_followup_work(f"Tool send_message: __MESSAGE_SENT__\n{text}")
            is True
        ), text


# --------------------------------------------------------------------------
# message_quota: premium is never below the configured free default
# --------------------------------------------------------------------------
def test_premium_allowance_is_never_below_the_configured_default():
    from message_quota import PREMIUM_MESSAGE_LIMIT, resolved_message_limit

    assert resolved_message_limit(150, None, "premium") == PREMIUM_MESSAGE_LIMIT
    # A default above the premium constant must not cap premium users lower.
    assert resolved_message_limit(900, None, "premium") == 900
    # An explicit per-user override still wins.
    assert resolved_message_limit(150, 42, "premium") == 42


# --------------------------------------------------------------------------
# sending: a whitespace run must not produce an empty chunk
# --------------------------------------------------------------------------
def test_split_response_never_emits_an_empty_chunk():
    from bot import MaxwellBot

    text = "a" * 30 + "\n" * 5000 + "b" * 30
    chunks = MaxwellBot._split_response(text, limit=100)
    assert chunks
    assert all(chunk.strip() for chunk in chunks)


# --------------------------------------------------------------------------
# inbox: an empty file reads AND writes as a fresh store
# --------------------------------------------------------------------------
def test_empty_inbox_file_is_writable(tmp_path):
    import asyncio

    from inbox import InboxStore

    store = InboxStore(str(tmp_path))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("", encoding="utf-8")

    async def run():
        assert await store.load_items() == []
        await store.add_notice(kind="email", summary="hello")

    asyncio.run(run())
    assert json.loads(store.path.read_text(encoding="utf-8"))["items"]


# --------------------------------------------------------------------------
# media_context: the provenance tag survives the cache round-trip
# --------------------------------------------------------------------------
def test_cached_media_keeps_its_source_tag():
    from bot import MaxwellBot

    class _Host:
        _media_context: dict = {}

    host = _Host()
    cache = MaxwellBot._cache_media_context.__get__(host)
    get = MaxwellBot._get_media_context.__get__(host)
    cache(
        "chan",
        [
            {
                "is_image": True,
                "b64": "AAAA",
                "mime_type": "image/png",
                "filename": "grid.png",
                "message_id": "1",
                "source": "emoji_grid",
            }
        ],
    )
    assert get("chan")[0]["source"] == "emoji_grid"


# --------------------------------------------------------------------------
# chess_game: the store honors the bot's DATA_DIR
# --------------------------------------------------------------------------
def test_chess_data_dir_honors_data_dir_env(monkeypatch, tmp_path):
    import chess_game

    monkeypatch.delenv("MAXWELL_DATA_DIR", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert chess_game._data_dir() == str(tmp_path)


# --------------------------------------------------------------------------
# watch_policy: the prompt states the operator's configured bar
# --------------------------------------------------------------------------
def test_describe_signal_uses_the_configured_threshold():
    from watch_policy import AddressSignal, WatchState, describe_signal

    text = describe_signal(
        AddressSignal(text_length=40), WatchState(), 0.12, 1000.0, threshold=0.5
    )
    assert "0.50" in text
    assert "no_response" in text
    # Comfortably above the configured bar: no marginal note.
    high = describe_signal(
        AddressSignal(text_length=40), WatchState(), 0.95, 1000.0, threshold=0.5
    )
    assert "no_response" not in high


# --------------------------------------------------------------------------
# tts/music URL parsing: a malformed port must not raise
# --------------------------------------------------------------------------
def test_cdn_url_rejects_a_malformed_port_without_raising():
    from plugins.tts_voice.clones import _cdn_url

    assert _cdn_url("https://cdn.discordapp.com:bad/x") is False
    assert _cdn_url("https://cdn.discordapp.com/attachments/1/2/x.png") is True


def test_music_identifier_reports_a_bad_port_as_a_music_error():
    import pytest as _pytest

    from plugins.music.service import MusicError, identifier

    with _pytest.raises(MusicError):
        identifier("https://youtube.com:99999/watch?v=dQw4w9WgXcQ")
