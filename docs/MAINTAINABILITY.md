# Maintaining Maxwell's shared services

Run the same dependency, lint, shell syntax, regression, and coverage checks used
in CI:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
make check
```

For an isolated interpreter, use `make check PYTHON=.venv/bin/python`.
`make test` runs the suite without coverage instrumentation. `make coverage`
measures lines and branches in `maxwell_core`, `message_pipeline`, and
`concurrency_safety` and requires at least 80% combined coverage. This is a
shared-core gate, not a claim that every Discord or external-service integration
is covered. Python 3.11 and 3.12 run the same checks in CI.

## Module ownership

| Concern | Implementation | Responsibility |
|---|---|---|
| Conversation prompts | `maxwell_core/prompts/conversation.py` | Stable instructions, authorized memory tiers, transcript, volatile context, live request |
| Prompt identity and protocol | `maxwell_core/prompts/protocols.py` | Code-owned instructions and model-controlled web grounding |
| Visible response cleanup | `maxwell_core/transport/output_safety.py` | Remove protocol leaks while preserving prose, links, and code |
| Prompt identity and time | `maxwell_core/transport/context_helpers.py` | Requester scope, live Discord identity, explicit Puerto Rico timestamps |
| Text attachments | `maxwell_core/transport/attachments.py` | Recognize and decode supported text with bounded output |
| Tool dispatch and result policy | `maxwell_core/tools/dispatch.py`, `results.py` | Circuit breaker, argument normalization, follow-up and delivery decisions |
| Provider wire formats | `maxwell_core/providers/` | SSE framing, completion parsing, custom tool buffers, usage, timing, and error rules |
| Provider requests | `providers.py` | OpenAI-compatible request construction, deadlines, retry and endpoint fallback |
| Plugin lifecycle | `maxwell_core/plugins/manager.py`, `lifecycle.py` | Setup, dependencies, rollback, teardown, reload, and resource ownership |
| Registry compatibility | `maxwell_core/tools/publication.py` | Refresh dynamic schemas without stealing built-in or another manager's entries |
| Scoped memory policy | `maxwell_core/memory/scope.py`, `identity.py` | Authorization and deduplication tied to provenance |
| Embedding validation | `maxwell_core/memory/embeddings.py` | Ordered, finite vectors that do not mutate caller buffers |
| Memory storage | `rag_memory.py` | SQLite persistence, authorized retrieval, and embedding recovery |
| Request lifecycle | `message_pipeline.py`, `concurrency_safety.py` | Bounded admission, fairness, cancellation, cleanup, and journal/watermark state |

`bot.py` remains the Discord composition root and turn coordinator. Existing
imports from `bot` and `providers` remain available; new helper callers should
import the owning module directly. The conversation builder accepts an explicit
host interface and immutable policy hooks, and imports without creating a bot.
It has separate methods for memory retrieval, transcript rendering, and the live
request.

## Lifecycle contracts

Publish core services before loading plugins. Synchronous setup can then resolve
`memory` and `provider:primary`. An async host calls `load_plugins()` followed by
`await complete_pending_setups()` before exposing tools or starting jobs.
Successful setup publishes tools; failure, timeout, or cancellation rolls back
that plugin and its dependent registrations. Use
`await reload_plugins_async()` for a running host. Core service and registry
objects retain their identities across reloads.

Cancelling a request stops its own work and frees its admission slot. Repeated
stop requests use `cancel_once()` so async cleanup can finish. A caller waiting
for a predecessor shields the predecessor's cleanup and still propagates its own
cancellation. A lock obtained before acquisition remains the channel's lock even
when idle lock entries are pruned.

Fact deduplication includes user, server, channel, and visibility provenance.
The `scoped_content_identity_v1` migration upgrades stored hashes atomically,
keeps row IDs, metadata, and embeddings, and records completion in
`memory_migrations`. Its temporary index bounds collision checks. Later opens
repair missing legacy hashes without rehashing the whole database. Visibility
rules remain fail-closed for legacy records without usable provenance.

## Regression coverage

New tests exercise real module behavior and local SQLite databases, including:

- Malformed, split, multiline, and CR/LF SSE; sparse tool indexes; callback failure;
  bounded stream reads; credential isolation; retry and context-limit recovery.
- Dependency failure and cycles; partial setup rollback; teardown without a hook;
  async reload; retired contexts; prompt and service ownership; schema refresh.
- Cross-user and cross-server fact identity; migration rollback and repeated open;
  malformed metadata; sanitized transcript patches; reordered embedding batches.
- Queue saturation, request replacement, repeated cancellation, timeout while
  waiting for admission, and cleanup of unrelated users' work.
- Zero history controls, scoped backend calls, cold embeddings, optional memory
  failure, stable transcript prefixes, and host-independent Puerto Rico time.
- Code/prose/link preservation, mixed protocol payload suppression, nested
  response envelopes, and seeded input invariants.

The suite uses synthetic provider responses and local storage; it does not need
production credentials. The optional real-provider progress test requires
`AI_BASE_URL`, and the browser-rendering test requires Chromium. Synthetic load
benchmarks cover 100/500/1,000 guilds and a 500,000-row pending-embedding database;
they measure local services rather than Discord or model throughput.

## Remaining large components

The main Discord turn loop, command handlers, some plugin implementations, and
the provider retry state machine are still substantial. The boundaries above
allow those to be changed in smaller pieces with behavioral regressions around
them. Keep permissions, visibility, model-selected search, and requester memory
scope in the same execution path when extracting further code.
