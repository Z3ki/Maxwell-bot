# Maxwell context and memory architecture

> Current-status replacement for the older line-number-based analysis. The previous version described an older Maxwell tree and contained claims that are no longer true, including that Maxwell had no vector/RAG memory and that memory lived in a separate `memory.py`. Use the implementation files linked below as the source of truth.

## Current architecture

Maxwell's memory/context system is layered rather than a single chat-history file.

### 1. Live Discord context

`bot.py` owns Discord ingestion and live turn assembly. It can use message text, reply metadata, attachments/media summaries, Discord context, recent conversation history, tool history, scoped facts, and configured persona/server context subject to runtime budgets and policy gates.

Not every observed message necessarily causes a reply. Reply policy, direct mentions/replies, DMs, channel restrictions, sleep/watch behavior, and runtime controls decide whether Maxwell should answer.

### 2. Vector/RAG memory

`rag_memory.py` contains `RAGMemoryManager`, the current memory manager. It is explicitly vector-backed and SQLite-backed.

It stores/retrieves multiple memory kinds, including channel/message memory, long-term facts, and scoped shared context. Embeddings use an OpenAI-compatible embedding endpoint. The bot can continue operating when embedding work is unavailable; the exact degradation/retry behavior is implemented in `rag_memory.py` and covered by tests.

This replaces the old statement that Maxwell had "zero vector DB, embedding, or RAG code." That statement is obsolete.

### 3. Scoped/cross-context facts

Cross-context memory is controlled by runtime settings such as `cross_context_enabled` and related extraction/visibility controls. Facts are scoped and filtered before they are injected into another turn.

Do not assume all remembered facts are globally visible. Private/admin/scoped context boundaries are part of the memory model.

### 4. Entity and relationship memory

`knowledge_graph.py` adds entity/relationship memory on top of conversation/fact storage. Runtime controls such as `entity_memory_enabled` and `knowledge_graph_enabled` determine whether these layers participate.

### 5. REM/background consolidation

`rem.py` provides optional REM-style memory consolidation. It is a background feature, separate from normal live message handling and separate from the primary chat provider configuration.

Simple installs keep token-spending background loops off by default. Runtime controls can also enable/disable background behavior after startup.

### 6. Context budgets

`context_budget.py`, runtime controls, and `bot.py` bound how much history/memory/tool context reaches a model turn. Current behavior should be read from those files rather than from old hard-coded line numbers in this document.

Important runtime controls include `prompt_context_budget`, `memory_context_budget`, `memory_history_messages`, and tool-history/context settings.

### 7. Inbound request journal

Directed inbound requests are tracked in `DATA_DIR/inbound_requests.sqlite3`. That database is for lifecycle/reliability state (IDs, timestamps, attempts, delivery state, reason labels), not a duplicate archive of full private Discord message content.

When recovery is possible, Maxwell can re-fetch message content from Discord instead of keeping a second complete copy in the request journal.

## Current files to inspect

| File | Why it matters |
|---|---|
| `bot.py` | Discord ingestion, reply policy, prompt/context assembly, memory integration |
| `rag_memory.py` | SQLite/vector memory and embedding-backed retrieval |
| `knowledge_graph.py` | Entity/relationship memory |
| `context_budget.py` | Context/prompt budgeting helpers |
| `rem.py` | REM-style consolidation |
| `control_defaults.py` | Memory/context runtime defaults |
| `api/state.py` | Runtime-control sanitization |
| `api/storage.py` | Dashboard/admin storage paths |
| `tests/test_entity_memory.py` | Entity-memory behavior |
| `tests/test_knowledge_graph.py` | Knowledge-graph behavior |
| `tests/test_web_rag_store.py` | RAG/web-memory behavior |
| `tests/test_memory_reload.py` | Reload/persistence behavior |
| `tests/test_ltm_batch.py` | Long-term-memory batch behavior |
| `tests/test_context_budget.py` | Context-budget behavior |

## Configuration

RAG/embedding configuration is documented in [docs/CONFIGURATION.md](docs/CONFIGURATION.md) and `.env.example`. `doctor.py --probe` can test the configured embedding endpoint when RAG is enabled.

The easy installer uses provider-neutral `AI_*` names for the primary chat model. Embeddings have their own advanced settings; do not assume the chat URL/model is automatically the embedding model.

## What is intentionally not frozen in this document

This file no longer duplicates exact source line numbers, fixed message counts, database column lists, or every scoring heuristic. Those details changed enough that the previous document became actively misleading.

For operational behavior, use the current code plus:

- [README.md](README.md)
- [docs/OVERVIEW.md](docs/OVERVIEW.md)
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- [docs/AUDIT.md](docs/AUDIT.md)

The repository tests are the best executable reference for edge cases.
