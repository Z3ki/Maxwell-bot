# Maxwell context and memory architecture

## Active storage and retrieval

`conversation_memory.py` implements the runtime `ConversationMemory` backend. It opens the existing `maxwell_rag.db`, retains historical fact/vector rows, and stores scoped Discord transcripts. Ordinary prompts do not run embedding generation, vector scoring, graph recall, automatic fact extraction or consolidation.

SQLite operations, message decoding and sanitization run on one FIFO worker. Writes and per-channel retention deletes are atomic; cancelling a caller does not interrupt an accepted transaction. Shutdown drains accepted work before closing SQLite. The stale-channel sweep uses the newest stored user message and the existing 12-hour inactivity cutoff; bot output does not keep a channel alive. Archived non-transcript rows are not deleted.

## Recent context and older recall

`maxwell_core/prompts/conversation.py` composes recent channel history as a stable prefix, then live-turn context. Reads require a validated `MemoryRequester` and exact channel/guild scope, including exact DM boundaries. Administrator status does not widen ordinary retrieval. Pointer hydration uses the same requester when Discord history is unavailable.

A contentless SQLite FTS5 index recalls older transcript passages matching meaningful words in the current query. Existing transcripts are indexed at startup; triggers update the index on inserts, edits, scope changes and deletions. Recall excludes messages already selected for recent context and the live turn. It does not search archived facts or vectors, cross channels, infer synonyms, or call an embedding/chat provider.

Older passages are optional, bounded reference material in a separate **user-role** `<recalled_conversation>` block. They are not system instructions or evidence of who owns a shared fact. At most four matches and 2,000 characters are added, within the remaining history budget. Final prompt trimming drops this optional block before recent conversation. Explicit app-selected history and disabled history do not receive extra recall; short ambient turns skip the lookup.

## Persistence and limits

`MEMORY_MESSAGE_LIMIT` controls per-channel transcript retention. There is no global channel eviction cap and no new aggregate storage quota. Recall cannot recover messages already removed by retention or the inactivity sweep. Existing ambiguous or unrelated archived data remains stored but is not included by this transcript-only path.

The active backend exposes channel/message statistics; it does not run a pending-embedding recovery worker. The administrator API remains an authenticated operational surface, not a community cross-scope memory API.

## Request reliability is separate

`message_pipeline.py` stores inbound lifecycle metadata in `DATA_DIR/inbound_requests.sqlite3`: IDs, timestamps, attempts, reason codes and delivery state, not a second private-message archive. Its own FIFO SQLite worker uses WAL and FULL durability. Irreversible-effect checkpoints are awaited before Discord/tool effects; delivery receipts are awaited afterward. Recovery re-fetches eligible messages from Discord rather than retaining their full content in the journal.

## Implementation and checks

- `conversation_memory.py`: storage, authorization, retention, FTS5 recall and worker lifecycle.
- `maxwell_core/memory/base.py`: requester and backend contracts.
- `maxwell_core/prompts/conversation.py`: stable recent prefix and optional older recall.
- `context_hydration.py`: requester-scoped fallback for reply/pointer context.
- `bot.py`: ingestion, prompt budgeting and shutdown.
- `tests/test_conversation_memory.py`, `tests/test_conversation_service.py`, `tests/test_context_hydration.py`, `tests/test_prompt_cache.py`: scope isolation, migration, edits/deletion, prompt boundaries and budget behavior.

A disposable clone of the live database (12,318 original rows) built the new index in 117.02 ms. Scoped lookups averaged 0.53 ms, maximum 1.70 ms; an older topic was recalled, another guild was excluded, the recent prefix stayed stable and original rows remained intact. These are local storage/prompt checks, not evidence of better model answers or an unbounded memory capacity.

See [docs/MEMORY_RELIABILITY.md](docs/MEMORY_RELIABILITY.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for runtime reliability and ownership.
