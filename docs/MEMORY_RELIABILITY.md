# Memory reliability changes

This note describes the reliability branch's current behavior and the decisions left open for a later memory redesign.

## Retention and storage

- The old global 25-channel eviction limit is removed. Activity in one channel cannot evict another channel's message history.
- Transcript retention remains a per-channel setting: `MEMORY_MESSAGE_LIMIT` (default 2,000; accepted range 1–10,000). Retention deletes only the oldest transcript rows in that same channel. It does not prune long-term facts or shared-context rows.
- There is no new aggregate customer-facing storage quota or permanent retention policy. Existing data is not removed by migration.
- The admin RAG stats endpoint reports channel, message, vector, embedded, and pending-embedding counts. The bot emits a privacy-safe embedding health summary at most once per minute with tracked background queue depth, pending work, processing time, failures, cache hit ratio, and endpoint cooldown. SQLite database file size can be measured by the operator for disk accounting.

## Scope and legacy data

Memory reads used in prompt assembly now require a validated `MemoryRequester`. Transcript and semantic retrieval are narrowed by channel and guild before vector decoding. Private user facts stay with their owner and source channel; guild facts require a public source channel; DM facts are DM-only. Persisted web search results carry the originating query's channel provenance, so private-channel and DM searches do not surface in guild channels. Cross-guild global facts are visible only when an administrator explicitly approved them as operator-public.

Rows whose original scope cannot be proven remain in SQLite and are hidden from prompt retrieval. This includes legacy unscoped LTM rows and graph edges without requester provenance. New chat-graph edges and their non-public nodes are namespaced to the exact source channel (or exact DM); public site-index edges remain shareable. Legacy graph edges remain stored but hidden. Administrator status does not widen ordinary semantic or transcript retrieval. The explicit global-recall tool returns only operator-approved public facts.

The LTM extractor and older REM/consolidation paths can still produce unscoped facts. Those writes are retained but fail closed in ordinary prompts. A later change should attach the originating channel/guild to extraction jobs before making their output retrievable. That needs a decision about which conversation scopes may be consolidated together.

## Retrieval and embeddings

RAG candidate rows are filtered in SQL by requester scope before vectors are loaded and scored. Scope and transcript indexes support the frequent filters. Scoring normalizes the filtered vectors into one NumPy matrix rather than repeatedly searching and decoding all rows per candidate.

The configured local embedding defaults remain `qwen3-embedding:0.6b`, 1,024 dimensions, at `http://localhost:11434`; `MAXWELL_EMBED_MODEL`, `MAXWELL_EMBED_DIM`, and `MAXWELL_EMBED_BASE_URL` can override them. Failed embedding generation leaves transcript text durable with a NULL vector. Query embedding has a bounded deadline and outage cooldown, so recent channel history remains available when semantic recall is offline. When local embeddings are enabled, a singleton background recovery worker runs an initial backlog pass at startup and retries durable NULL-vector rows every 60 seconds. It uses bounded batches, yields to interactive embedding requests, snapshots each pass, and stops cleanly during shutdown. A partial SQLite index narrows pending-row scans; metrics expose pending work, active rows, and whether the worker is running.

The synthetic benchmark compares the prior broad blank-scope LTM query and Python vector loop with the new scoped RAG path. It calls no Discord, embedding, or chat provider. Its numbers describe SQLite/filtering and vector-scoring behavior on the CI runner, not live inference throughput or a server-capacity guarantee.

## Confirmed follow-up risks

- SQLite calls are synchronous and run on the bot's event loop. Scope filtering reduces rows substantially, but very large per-scope corpora or slow storage still need profiling before deciding whether to move database work off-loop.
- The current Docker deployment still mounts `/var/run/docker.sock:ro`. A read-only bind mount does not make Docker API operations read-only. The site builder depends on host file visibility, so replacing this access with a narrower helper or separate builder changes the hosting boundary and should be reviewed separately.
- The API memory routes require administrator authentication. They remain operational/admin interfaces and are not requester-context endpoints for community users.

## Future memory design

Keep the current SQLite store until production measurements show it cannot meet the workload. A later redesign can split small interfaces for ingestion, scope authorization, durable storage, embedding, retrieval, consolidation, retention, and context assembly while preserving the authorization tests in this branch. Before moving or widening data, decide:
1. Whether explicitly approved public facts may be shared across guilds, and who can approve them.
2. Whether consolidation may combine channels, guilds, or DMs.
3. How legacy ambiguous rows will be reviewed or mapped to a scope.
4. What disk-growth and retention controls operators need, without silently deleting existing memories.
5. Whether SQLite's measured write contention and event-loop cost justify a different store or a worker boundary.
