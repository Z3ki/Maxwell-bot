# Maxwell reliability research — historical snapshot and current status

The original version of this file was a deep reliability review dated **2026-07-10**. It was useful at the time, but it described an older codebase as if it were still current. Several central claims in that review are now obsolete.

This file is retained as a research entry point, not as a source of current architecture truth. For current deployment/runtime behavior, start with [README.md](README.md), [docs/OVERVIEW.md](docs/OVERVIEW.md), [docs/CONFIGURATION.md](docs/CONFIGURATION.md), and the newer [docs/AUDIT.md](docs/AUDIT.md).

## Major historical claims that are no longer current

### Maxwell is not a Discord self-bot

Current Maxwell expects an official Discord bot token from the Discord Developer Portal. User/self-bot tokens are not supported.

### Native tool calls are now supported and preferred

The July review described Maxwell as intentionally rejecting provider-native `tool_calls` and relying on custom XML/pipe-tag orchestration. That is no longer the default architecture.

Current runtime controls prefer OpenAI-style native `tool_calls` (`native_tool_calls: true`) when the provider supports them. A compatibility fallback remains for models/endpoints that do not return structured calls correctly.

`tool_schemas.py`, `providers.py`, `bot.py`, `jobs.py`, and the tool-call tests are the current references.

### RAG/vector memory exists

The old research predates the current `RAGMemoryManager`. `rag_memory.py` now provides SQLite/vector-backed memory and embedding retrieval. `knowledge_graph.py` provides entity/relationship memory.

### Intel/Pi-era descriptions are historical

The July document discussed older Intel/background-ingestion and Pi/Node experiments as part of the active reliability picture. Those details should not be used to infer the current runtime. The current tree, tests, and newer audit are authoritative.

### Runtime/deployment moved to Docker-first supervision

Current supported deployment runs Maxwell in Docker with bot/API supervision in the container. Historical PM2/host-process notes are relevant only when migrating old installs.

## Current reliability mechanisms

The current codebase includes a broader set of explicit reliability controls than the July snapshot:

- Directed inbound request lifecycle journaling in `DATA_DIR/inbound_requests.sqlite3`.
- Bounded retries that avoid automatic replay after uncertain external side effects.
- Whole-turn and tool-iteration timeouts.
- Centralized runtime-control sanitization.
- Provider retry/fallback routing and endpoint/model timing diagnostics.
- Native tool-call normalization and regression coverage.
- Progress-message ownership/cleanup for tool-backed responses.
- Persistent owner/dashboard runtime controls.
- Corrupt-state preservation/backup logic in audited storage paths.
- Container supervision and bounded shutdown behavior.
- Autofix test isolation that fails closed when the configured test container cannot be used safely.

See [docs/AUDIT.md](docs/AUDIT.md) for the 2026-09 audit snapshot and its explicitly listed remaining risks.

## Current tool-output protections

Maxwell still contains compatibility parsing/cleanup because provider/model output is not perfectly uniform, but the preferred path is structured provider-native tool calls.

The `maxwell_extras` plugin also includes visible-output guarding and rich `/maxwell` response handling. Tool-backed app commands use a stable progress state after roughly 10 seconds, then send the final result as a follow-up rather than overwriting the answer at the last moment.

## Current prompt-injection/tool boundary

Tools marked destructive are blocked when the current turn has been tainted by fetched/web content. A fresh user message starts a clean turn. The old manual-confirmation override flow was removed; the taint gate is fail-closed for that turn.

This reduces one class of indirect prompt-injection risk but does not make arbitrary web content trusted. Review [SECURITY.md](SECURITY.md) before enabling powerful tools in hostile/public environments.

## Current state and persistence caveats

Some general reliability lessons from the original research still apply even though many concrete examples were repaired:

- External APIs and Discord sends cannot provide perfect exactly-once semantics across an unknown crash boundary.
- The Docker socket is a high-trust capability.
- Generated pages/browser probes must be isolated from sensitive admin origins/networks.
- Cross-process or multi-writer state deserves explicit locking/transaction design.
- Provider behavior differs across OpenAI-compatible implementations and needs real endpoint testing, not only unit mocks.

The newer audit documents which of these were repaired and which remain deployment risks.

## How to investigate a current production problem

Use current evidence rather than old line numbers:

1. Record the affected Discord message ID, channel ID, and timestamp.
2. Check inbound lifecycle state/logging and the request journal.
3. Record the effective `bot_control.json` settings.
4. Record the actual primary/fallback model and endpoint selected for the attempt.
5. Check Docker logs and `doctor.py` output.
6. Reproduce against the current commit and relevant regression tests.
7. Only then compare source-history commits around the observed regression window.

Useful commands:

```bash
docker compose logs -f maxwell
docker compose exec maxwell python3 doctor.py
docker compose exec maxwell python3 doctor.py --probe
```

For code changes:

```bash
ruff check .
python -m pytest
```

## Historical record

If you need the exact July 10 analysis, retrieve the pre-refresh version from Git history. Keeping its old source line numbers and assertions in the live README surface was more harmful than useful because the repository architecture changed substantially after that review.
