# Maxwell Comprehensive Improvement Plan

**Scope:** Code quality, security hardening, reliability, performance, test coverage, and operational hygiene for the Maxwell Discord self-bot.  
**Goal:** Make the codebase safer, more maintainable, and more observable without breaking existing behavior, then restart via PM2 and push to the repo.

---

## 1. Executive Summary

Maxwell is a feature-rich Discord self-bot (~19 kLOC Python). The current architecture is heavily monolithic: `bot.py` (6.6 k lines) owns Discord events, Telegram, tool orchestration, media processing, voice, REM/autonomy/context-cleanup scheduling, and admin commands. `bot_tools.py` (3.1 k lines) implements 30+ LLM-callable tools. `autonomy.py` (2.4 k lines) runs self-directed decision loops. This concentration makes the code hard to reason about, test, and secure.

The highest-impact improvements are:
1. **Security:** tighten file-path validation in `CreateSiteTool`/`SendFileTool`, harden `ShellTool` command allow-listing, and add input sanitization to admin commands.
2. **Reliability:** add structured error handling, graceful shutdown, persistent save flushing, and resource cleanup for media pipelines.
3. **Maintainability:** split `bot.py` into focused modules (message handling, media processing, commands, voice, lifecycle), deduplicate helpers, and add type hints.
4. **Performance:** stream/bound media processing, cap in-memory JSON stores, and reduce blocking I/O in async paths.
5. **Testing:** add adversarial tests for security-critical tools and expand coverage for REM/autonomy/API auth.
6. **Operations:** add a health endpoint, structured logging, and PM2-safe restart procedure.

This plan is designed to be implemented in small, reviewable PRs so the bot stays online and each change can be rolled back independently.

---

## 2. Current State (from audit)

### 2.1 Architecture
- **Entry points:** `bot.py` (Discord/Telegram bot), `api/api_server.py` (admin dashboard).
- **Core modules:** `bot_tools.py`, `autonomy.py`, `providers.py`, `memory.py`, `context_cleanup.py`, `rem.py`, `voice_live.py`, `config.py`, `utils.py`, `control_defaults.py`.
- **Tests:** 19 pytest files, many small; security-critical paths are largely untested.
- **Commands:** empty `commands/` package; all `,` commands live inline in `bot.py`.

### 2.2 Biggest files by line count
| File | Lines | Responsibility |
|------|------:|----------------|
| `bot.py` | 6,623 | Main bot, events, orchestration, media, voice, admin commands |
| `bot_tools.py` | 3,112 | All LLM tools |
| `autonomy.py` | 2,410 | Self-directed agency engine |
| `api/api_server.py` | 1,914 | Dashboard / admin HTTP API |
| `memory.py` | 799 | Short/long-term memory + shared context |
| `context_cleanup.py` | 683 | Shared-context janitor |

---

## 3. Prioritized Improvements

### Phase A — Security hardening (highest priority, do first)

| # | Change | File(s) | Effort | Risk if ignored |
|---|--------|---------|--------|-----------------|
| A1 | **Contain `CreateSiteTool` image paths.** Currently `src_path` is accepted from the LLM and only checked with `os.path.isfile`. Validate it is under `MAXWELL_SITE_DIR` or an explicit allow-list, and reject symlinks outside the target. | `bot_tools.py:1566-1586` | small | Arbitrary file read via LLM prompt |
| A2 | **Harden `SendFileTool`/`SendMediaTool` filenames and extensions.** Whitelist extensions, sanitize filenames with `Path.name`, reject `..` and absolute paths, and never silently append `.png` to non-image data. | `bot_tools.py:1847+, 2752+` | small | Path traversal / malicious file upload |
| A3 | **Add `ShellTool` command allow-list / deny-list.** Parse the command and reject host-escaping patterns (`--privileged`, `--volume`, bind-mounts, `/var/run/docker.sock`). Consider a configurable regex allow-list. | `bot_tools.py:1906-2168` | medium | Docker sandbox escape / host RCE |
| A4 | **Sanitize admin command arguments.** Commands like `,blacklist`, `,context forget/private/global`, `,autonomy interval` parse user input; validate IDs are numeric and scopes are known. | `bot.py` command handlers | small | Command injection / privilege abuse |
| A5 | **Do not reload `.env` into `os.environ` in `api_server.py`.** Use a read-only config object; mutating `os.environ` leaks values to child subprocesses. | `api/api_server.py:30-58` | small | Secret leakage to shell/yt-dlp/etc. |
| A6 | **Add constant-time auth to all API routes.** `/api/auth/discord` routes bypass Basic auth; verify the Discord token path is also rate-limited and origin-checked. | `api/api_server.py:118-249` | medium | Brute force / OAuth token replay |

### Phase B — Reliability & correctness

| # | Change | File(s) | Effort | Risk if ignored |
|---|--------|---------|--------|-----------------|
| B1 | **Flush debounced saves on SIGTERM.** Memory/REM/context-cleanup use 5-second debounced `call_later` saves; add `atexit`/signal handlers to `flush()` before shutdown. | `memory.py`, `autonomy.py`, `context_cleanup.py`, `bot.py` | small | Data loss on PM2 restart |
| B2 | **Replace broad `except Exception` with specific handling.** Log stack traces for unexpected errors; only swallow known-safe exceptions. | `bot.py`, `bot_tools.py`, `autonomy.py` | medium | Silent failures, hard to debug crashes |
| B3 | **Add graceful shutdown to `bot.py`.** Register `SIGTERM`/`SIGINT` handlers, close provider session, flush memory, stop autonomy/REM/context-cleanup loops, then disconnect Discord. | `bot.py` | medium | PM2 kill_timeout exceeded, data loss |
| B4 | **Guard `aiohttp.ClientSession` lifecycle.** Ensure `_get_shared_session()` and `OllamaProvider._get_session()` are closed on shutdown and not recreated after close. | `bot_tools.py:131-145`, `providers.py:164-179` | small | Resource leaks / connector exhaustion |
| B5 | **Tighten provider retry state.** The `max_tokens` clamp mutation in `generate_chat_completion` is hard to follow; store clamped value per-call and log every mutation. | `providers.py:341-358` | small | Infinite retry loops, unexpected billing |
| B6 | **Validate JSON store shapes on load.** Several `_load_json_safe` fallbacks silently replace corrupt files with defaults; keep a `.corrupt` backup for recovery. | `memory.py`, `utils.py` | small | Silent data loss |

### Phase C — Maintainability / refactoring

| # | Change | File(s) | Effort | Risk if ignored |
|---|--------|---------|--------|-----------------|
| C1 | **Extract admin command handlers from `bot.py`.** Move `,prompt`, `,clearmem`, `,autonomy*`, `,blacklist*`, `,context*`, `,rem*`, `,vc*` into `commands/` modules or `bot_commands.py`. | `bot.py`, new `bot_commands.py` | large | `bot.py` becomes unmanageable |
| C2 | **Extract media processing helpers.** Move video normalize, frame/audio extract, GIF sheet, embed download, TTS into a `media.py` module. | `bot.py`, new `media.py` | medium | Duplication, hard to test |
| C3 | **Extract voice logic.** Move VC join/leave/listen/say into `voice_live.py` or a new `voice_manager.py`. | `bot.py`, `voice_live.py` | medium | Tight coupling |
| C4 | **Deduplicate helpers.** `_utcnow_iso`, `_load_json_safe`, `_truncate`, `_coerce_utc_datetime`, mention rendering exist in multiple files; consolidate in `utils.py`. | `bot.py`, `autonomy.py`, `context_cleanup.py`, `memory.py`, `utils.py` | small | Drift, bugs from copy-paste |
| C5 | **Add module-level constants for magic numbers.** Replace scattered limits (1990, 1900, 8000, 600, 500, etc.) with named constants. | all | medium | Hard to tune, accidental regressions |
| C6 | **Add type hints to hot functions.** Especially `MemoryManager`, `OllamaProvider`, `Tool` dispatch, and command handlers. | all | medium | Runtime errors from wrong shapes |

### Phase D — Performance

| # | Change | File(s) | Effort | Risk if ignored |
|---|--------|---------|--------|-----------------|
| D1 | **Bound memory usage for media.** Read attachments in chunks, avoid full Base64 round-trip when possible, and cap total media bytes per message. | `bot.py:4350-4430` | medium | OOM on large video/image |
| D2 | **Offload ffmpeg/video work to a thread pool.** `await asyncio.to_thread(...)` around the blocking `read_bytes`/`write_bytes` and subprocess waits, or use a bounded process pool. | `bot.py`, `bot_tools.py` | medium | Event loop blocking |
| D3 | **Cap `_spotify_seen`.** Already partially capped at 5000; ensure it stays capped and consider TTL. | `bot.py:3193-3227` | tiny | Unbounded memory growth |
| D4 | **Paginate API list endpoints.** `channel_list` and `chat_history` load full `memory.json`; add limit/offset parameters and default caps. | `api/api_server.py:1612-1636` | small | API OOM on large memory files |
| D5 | **Increase `max_memory_restart` or make configurable.** 1 GB is tight for a bot that base64-encodes multiple 25 MB videos. | `ecosystem.config.js:46` | tiny | PM2 restarts under load |

### Phase E — Testing

| # | Change | File(s) | Effort | Risk if ignored |
|---|--------|---------|--------|-----------------|
| E1 | **Add SSRF tests for `_is_safe_url` and `_SafeResolver`.** Verify DNS rebinding, IPv6, octal/hex IP, and redirect-after-check scenarios. | `tests/test_security_ssrf.py` | small | SSRF regressions |
| E2 | **Add path-traversal tests for `SendFileTool`, `SendMediaTool`, `CreateSiteTool`.** | `tests/test_security_paths.py` | small | File read/write escapes |
| E3 | **Add `ShellTool` command-normalization tests.** Verify it rejects `;`, backticks, `--privileged`, and Docker socket mounts. | `tests/test_shell_security.py` | small | RCE regressions |
| E4 | **Add API auth tests.** Test Basic auth bypass, rate limiting, missing creds, and Discord token expiry. | `tests/test_api_auth.py` | small | Admin API compromise |
| E5 | **Add REM/autonomy loop tests.** Verify start/stop/flush, state persistence, and goal limits. | `tests/test_autonomy_engine.py`, `tests/test_rem_loop.py` | medium | Silent breakage in background loops |
| E6 | **Run full pytest suite after each phase.** Add GitHub Actions or local CI step. | `pytest.ini`, `.github/workflows/ci.yml` | small | Regressions slip through |

### Phase F — Operations / observability

| # | Change | File(s) | Effort | Risk if ignored |
|---|--------|---------|--------|-----------------|
| F1 | **Add `/api/health` endpoint.** Return bot connectivity, provider availability, PM2 status, and last error. | `api/api_server.py` | small | No way to monitor liveness |
| F2 | **Add structured JSON logging option.** Keep current human-readable default but add `LOG_FORMAT=json` env var for log aggregation. | `bot.py`, `api/api_server.py` | small | Hard to alert on errors |
| F3 | **Document restart procedure and rollback.** Include `pm2 save`, `pm2 logs`, and `git revert` steps. | `README.md` | tiny | Bad restarts, downtime |
| F4 | **Add `.env.example` and `.gitignore` checks.** Ensure `data/`, `logs/`, `.env`, `__pycache__`, and generated sites are ignored. | `.gitignore`, `.env.example` | tiny | Secret/data leaks |

---

## 4. Implementation Order

Each phase is a separate commit/PR so the bot can be restarted and validated between steps.

1. **PR 1 — Security hotfixes** (A1–A6). Small, high-impact, low risk of behavior change. Restart after merge.
2. **PR 2 — Reliability** (B1–B6). Graceful shutdown and save flushing. Restart and watch for clean shutdown.
3. **PR 3 — Test scaffolding** (E1–E4). Add adversarial security tests before big refactors so regressions are caught.
4. **PR 4 — Refactor extraction** (C1–C3). Move commands/media/voice into modules. Restart and smoke-test all commands.
5. **PR 5 — Cleanup & constants** (C4–C6, D3, F1–F4). Deduplicate helpers, add health endpoint, structured logging.
6. **PR 6 — Performance** (D1, D2, D4, D5). Media bounds and thread/process pools. Restart under load.

**Total estimated effort:** ~2–3 days of focused work, split across 6 PRs.

---

## 5. Deployment & Verification Plan

### Before any code change
1. `git status` — confirm working tree is clean.
2. `git log --oneline -5` — note current HEAD.
3. `pm2 status` — record current uptime/restarts.
4. Run existing tests: `pytest -q` and record baseline.

### Per-PR restart procedure
1. Merge PR locally or on `main`.
2. `pytest -q` — all tests must pass.
3. `git pull` on the production checkout if needed.
4. `pm2 restart ecosystem.config.js` (uses 15 s kill_timeout for graceful bot shutdown).
5. `pm2 logs --lines 50` — verify both `maxwell-bot` and `maxwell-api` come online.
6. Spot-check one command and one dashboard endpoint.

### Final push
1. After all PRs pass and the bot is stable, `git push origin main`.
2. Run `pm2 save` if process list changed.
3. Update `progress.md` with what landed.

### Rollback
- Any PR can be reverted with `git revert <commit>` and `pm2 restart ecosystem.config.js`.
- Data files are not touched by these changes except through existing atomic-write paths.

---

## 6. What I Need From You

Please confirm:
1. **Approve this overall plan?** (Then I’ll start with PR 1 — security hotfixes.)
2. **Restart and push authorization?** I can restart PM2 and push after each PR, or batch them and do one final restart/push.
3. **Any off-limits areas?** For example, do you want me to avoid touching the autonomy/REM prompts or keep the shell tool exactly as-is?
4. **Environment access:** Is `pm2` available in this shell and is the repo already on the branch you want to push?

Once approved, I’ll switch out of plan mode and begin implementation.
