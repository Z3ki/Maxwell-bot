# Memory reliability

## Storage and retention

The active `ConversationMemory` backend uses the existing `maxwell_rag.db`. Historical facts, vectors and unrelated rows remain stored; the active prompt path reads transcripts only. It does not require an embedding endpoint or run local vector inference, fact extraction, graph recall or consolidation.

There is no global channel-eviction limit. `MEMORY_MESSAGE_LIMIT` bounds transcripts per channel; retention removes only that channel's oldest transcript rows. The inactivity sweep atomically removes transcripts from channels whose newest stored user message is older than 12 hours. Bot output does not extend the cutoff. Neither operation deletes archived non-transcript rows or messages on Discord. No new aggregate storage quota is imposed.

All runtime SQLite reads/writes, row decoding, sanitization and cleanup run on one FIFO worker, off Discord's event loop. Whole writes and retention deletes stay in one transaction. Caller cancellation does not revoke accepted work; shutdown drains the worker before closing the connection. A covering partial index avoids the prior broad cleanup scan.

## Authorization and older recall

A validated `MemoryRequester` is required for prompt reads. SQL constrains both channel and guild before returning transcripts, including exact DM scope. Admin status does not widen retrieval. Discord pointer/reply hydration uses the same requester for its stored-history fallback.

A contentless FTS5 index searches older messages using bounded meaningful query terms. Startup seeds existing transcripts; SQLite triggers maintain the index on insert/edit/scope change/delete. Recall excludes already-selected recent messages and does not search facts, vectors, other channels or other guilds. Archived ambiguous rows remain stored but are not newly exposed.

Recent history stays the cacheable prefix. Up to four older matches, at most 2,000 characters within the remaining history allowance, follow in a separate user-role `<recalled_conversation>` reference block. Retrieved text is not an instruction or persona override. Optional older recall is trimmed before recent conversation. Disabled history and explicit app-selected history do not receive additional recall; short ambient turns skip it. Lexical recall does not establish semantic equivalence or recover pruned messages.

## Evidence and operational boundaries

A read-only snapshot copied into a disposable container preserved 12,318 original rows. The new index initialized in 117.02 ms; scoped retrieval averaged 0.53 ms and peaked at 1.70 ms. The run demonstrated older-topic recall, cross-guild exclusion and a stable recent prompt prefix. Earlier cleanup checks on a separate 12,287-row clone averaged 13.69 ms, maximum 17.06 ms, with event-loop heartbeat oversleep at most 4.22 ms. These measurements do not establish unlimited corpus size, live model answer quality or total VPS capacity.

The inbound request journal is separate: a FIFO worker, WAL and FULL durability, with awaited checkpoints before irreversible effects and awaited delivery receipts. A real 350 ms SQLite write lock delayed its checkpoint by 442.29 ms while heartbeat oversleep stayed at most 3.75 ms; delivery did not proceed before the durable checkpoint.

The admin API remains authenticated and operational, not a requester-context cross-scope endpoint. Measure database size and retention when growing deployments. Production's existing Docker socket mount is a separate hosting-security concern; a read-only mount does not make Docker API operations read-only. The isolated Dev deployment does not mount that socket or production data.
