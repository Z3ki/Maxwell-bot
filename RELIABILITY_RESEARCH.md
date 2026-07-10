# Maxwell Bot - Deep Reliability Research (2026-07-10)

## Executive Summary
Maxwell is a ~7k+ LOC Discord self-bot (bot.py monolith) driven by OpenAI-compatible LLMs. It features rich multimodal support, ~30+ custom tools, autonomy (self-directed actions), REM "dreaming" (memory consolidation), Intel (background knowledge ingestion), shared context, context cleanup, dashboard/API (separate process), and experimental Pi brain work.

**Core unreliability stems from:**
1. **Fragile custom tool-calling** (XML/pipe tag parsing + post-hoc stripping) instead of native tool_calls. This directly causes "leaks" (internal artifacts, reasoning, tags appearing in user-visible output) and bad logic when models deviate from the strict format.
2. **Cross-process shared-state races** between bot.py and api/api_server.py on JSON files (bot_commands.json, autonomy_*.json, rem_*.json, etc.).
3. **Autonomy engine logic gaps** (documented extensively in subagents/autonomy.md) allowing spam, bypass of controls, misreported success/failure, missed events.
4. **Incomplete resource / task / shutdown hygiene** leading to stuck states (e.g. REM "running"), leaked subprocesses, partial cleanups, and observed Node core dumps (`core.*` files from Pi/Node experiments).
5. **Monolithic complexity + heuristic sanitizers** (hundreds of lines of regex in bot.py for leaks) that are "supposedly fixed" (extensive tests in test_tool_calls.py) but inherently incomplete against model variance.
6. **Persistence durability and recovery inconsistencies**.

The "leaks" user mentions refer primarily to `strip_tool_payload_leaks`, `strip_model_artifact_leaks`, and related regexes (bot.py:878-1149+) that attempt to remove `<|tool_...|>`, `<tool:send_message>..</tool>`, JSON reasoning blocks, `<system-reminder>`, `<environment_details>`, etc. from final responses. Despite many variants and tests, models (especially local/fine-tunes) still emit new forms or interleave text+tools badly.

Recent partial mitigations exist (watermark fixes in autonomy, more strip variants, corrupt-file backups), but systemic issues remain.

## Key Architecture
- **Main loop**: `bot.py:MaxwellBot` (~7250 LOC)
  - `on_message` → filters → `_handle_message` (5453+)
  - Builds rich system+memory+context prompt via `_build_messages` (6137+)
  - `_tool_system_prompt` (5949) injects tool list + **strict "XML text tags only" instructions**.
  - `ai_provider.generate_response` (providers.py) → rejects native `tool_calls` with explicit comment: "pretending they are text is how tool orchestration gets cursed."
  - Tool loop: up to `max_tool_iterations`, `_process_tool_calls` (5779) uses `collect_tool_calls` + `_iter_top_level_tool_tags` (complex regex soup for XML/pipe/self-closing/unclosed variants).
  - After tools: heavy post-clean (strip, remove markers, split, send).
- **Providers** (`providers.py:522 LOC`): OpenAI /v1 compat + multimodal. Good retry + fallback + cooldown logic. Supports `tools=` in payload but main path forbids consuming `tool_calls`.
- **Tools** (`bot_tools.py:3419 LOC`): Many classes implementing `execute`. Includes side-effecting Discord ops, shell (Docker sandbox), web/yt/image gen, create_site (writes public HTML), memory ops.
- **Autonomy** (`autonomy.py:2640 LOC`): Periodic LLM planner for JSON actions (post_channel, run_tool, etc.). Gathers context, plans, executes. Separate provider path.
- **REM** (`rem.py:183 LOC` + scheduler in bot): Background memory assimilation using visible events ring (`data/rem_events.json`).
- **Memory/Context** (`memory.py`): Per-channel short-term, long-term txt, scoped shared_context, RemEventLog. Atomic writes via utils.
- **ContextCleanup** (`context_cleanup.py:1001 LOC`): LLM-driven dedup of shared facts.
- **Intel**: Background RSS/news → long-term memory facts.
- **API/Dashboard**: Separate aiohttp process. Queues commands via shared `bot_commands.json` (polled by bot).
- **State**: Dozens of JSONs in `data/`. Atomic write helpers (temp+fsync+replace) in utils.py + copies.
- **Pi experiments**: Branches + pyc remnants + .kilo; multiple recent Node core dumps (`core.NNNNN` ~60MB each) indicate subprocess instability/leaks.

## Detailed Unreliability Findings (with refs)

### 1. Tool Format Leaks & Bad Output Logic (Primary "leaks" complaint)
- **Root cause**: Intentional avoidance of native tool calling (providers.py:250-253 comment). Model must emit exact custom XML described in _tool_system_prompt (bot.py:5960+):
  - Rules: "Output either visible text or tool tags, never both", "must end with exactly one terminal action: send_message or no_response".
  - Models frequently violate: emit reasoning JSON first, interleave, use pipe tokens `<|tool_...|>`, unclosed tags, nested, `<tool_send_foo>`, `<function>`, leftover after strip.
- **Mitigation code** (heuristic, high maintenance):
  - `strip_tool_payload_leaks` (1129), `strip_model_artifact_leaks` (910), `_strip_leading_reasoning_json` (891).
  - Dozens of regex: PIPE_TOOL_RE, GENERIC_PIPE_TOOL_RE, ARTIFACT_BLOCK_RE, LEAKED_*, TOKEN_*, UNTERMINATED_*, etc. (bot.py ~850-890).
  - `_iter_top_level_tool_tags` (942) + parsers for 5+ syntaxes + fenced-code skipping + body param extraction (1030+).
  - Post-processing in _handle (5669+), _process_tool_calls (5784+), many call sites.
- **Evidence of "supposedly fixed"**: Extensive `tests/test_tool_calls.py` covering self-closing, pipe, unclosed + <|end|>, JSON+reminder, shorthand, underscore prefix, disabled, etc. (20+ tests).
- **Why still fails**: New model outputs, reasoning in final turn, partial tags, prompt injection of tags, very long responses, different tokenizers.
- **Impact**: Visible garbage in Discord replies, broken tool loops (stuck iterations?), polluted tool result injection, user confusion.
- **Related**: Pre-auto YT/web_search hacks (5493-5539) that inject before model sees message.

**Recommendation**: Migrate to native `tools` + tool_calls loop (different message formatting/loop). Or enforce JSON tool-plan via response_format + separate visible answer. Or stronger "think then act" with final visible-only turn.

### 2. Cross-Process State Races & Lost Updates (High reliability impact)
- API (api_server) appends to bot_commands.json under local `_file_lock` (api:1350).
- Bot command_queue_loop (bot.py:4408+) reads full file, sleeps during long work (autonomy/REM/tool calls), rewrites full list (2893-2995).
- Any append during the window is lost when bot writes its snapshot.
- Same pattern for autonomy goals/logs, REM control/runs, possibly others.
- Subagents/persistence.md explicitly calls this a "Blocker".
- Corrupt handling: bot backs up and resets (good), but some paths (autonomy via API) can clobber to empty on bad read.
- Atomic writes good (utils.py:30-50: mkstemp, dump, flush, fsync, replace) but **no fsync on parent dir** (power-loss risk) and duplicated in 5+ places.
- CreateSiteTool writes HTML directly then metadata (risk of orphan on crash).

**Refs**: bot.py:2886-2997 (queue), 4417; api/api_server.py:1289-1359, 545; autonomy.py load/save; persistence.md; test_api_corrupt_writes.py.

**Fix priority**: Introduce shared locking (fcntl on Linux) or move to SQLite/append-only log for commands. Or bot does compare-and-merge on write.

### 3. Autonomy Logic & Correctness Issues (Bad logic user complaint)
Detailed audit in subagents/autonomy.md (many "Blocker").

Key open/recently touched:
- **Same-tick channel spam**: `_parse_plan` checks cooldowns *before* any updates; `_exec_post_channel` updates *after* send. Multiple `post_channel` for same CID all execute. (autonomy.py:886-913, 1086-1113). "BUG FIX" comments around 823/2587 but cooldown bypass remains.
- **Watermark / missed events**: Recent change to use `tick_start_iso` (826) for `last_tick` so events *during* plan/execute are not dropped on next drain. Still subtle races possible (gather vs record timing).
- **Bypass of bot controls**:
  - Ignores `tools_enabled` / `disabled_tools` (uses only AUTONOMY_DISABLED_TOOLS hardlist).
  - Ignores `blocked_channels`, `allowed_channels`, `bot_enabled`.
  - `run_tool` can call `send_message` etc. bypassing post_channel cooldowns/validation (SyntheticMessage).
- **Error misclassification**: Many tools return "Error: ..." strings; `_exec_run_tool` treats any str return as success. (1178)
- **Goal limits & API drift**: Store caps at 50 but API and exec don't enforce consistently.
- **Backoff dead**: consecutive_failures incremented only on exception from tick(), but tick() catches everything. (autonomy.py:383)
- **Shared context**: autonomy dumps raw dicts or all facts without visibility filter (get_relevant...).
- **Fallback channels**: nondeterministic set order for missing target_channel_id.
- **No upper bound on interval** (can sleep days).
- Autonomy planner/executor does not see the same gates as normal chat.

**Refs**: autonomy.py:66 (AUTONOMY_DISABLED_TOOLS), 345-371 (loop), 651 (plan), 886 (_parse), 915 (validate), 1086 (exec), 1124 (run_tool), 1178; bot.py normal gates 1386+ , 3745+; subagents/autonomy.md full.

Also: autonomy can run shell/kilo/create_channel etc. (security.md).

### 4. Resource Leaks, Shutdown, Runtime Hygiene
From subagents/runtime.md + code:
- **REM stuck "running"**: `asyncio.CancelledError` is not caught as Exception in some paths (bot.py:2438 REM guarded, rem.py). Persistent state not cleared on abrupt cancel/shutdown → API refuses new runs.
- **Shutdown incomplete**: `bot.close()` / main finally (4357+) cancels `_tasks` but not all `_active_requests`, `_context_tasks`, voice sinks, etc. before closing sessions/providers. (runtime.md #11)
- **No SIGTERM handler**: Only KeyboardInterrupt. PM2/systemd kills may skip flushes (memory, REM events).
- **PM2 config gaps**: No kill_timeout, uses system python3 not venv, env merge order, etc.
- **Subprocess leaks**: ShellTool/Docker, autonomy tool timeouts (30s wait_for but no kill in finally for some), pm2 helpers in API. (bot_tools.py:1536+)
- **VC bypass**: Still has separate `_vc_ai_semaphore`; older notes said it bypassed global AI slot (some fixes applied 3156+).
- **Provider sessions**: Explicit close tracking for autonomy_provider churn to avoid aiohttp leaks (good, 1789+).
- **Observed**: Multiple 60MB+ Node core dumps today (from `pi` / .kilo / experimental-permission node processes). Indicates OOM/crash in sidecar agent experiments.
- Task tracking scattered (`_tasks`, per-channel, vc_active_tasks).

**Refs**: bot.py:1787 (provider close), 2438-2467 (REM), 4357-4398 (shutdown), 3617 (active), voice_live.py, autonomy.py timeouts, ecosystem.config.js.

### 5. Other Logic / Fragility
- **Prompt bloat & drift**: System prompt assembled from many sources (personality, LTM recent, shared context, users, emojis, tools, media, custom server prompt, drug mode, jailbreak). Context budgets are char-based hacks. Autonomy uses different formatting (raw dicts).
- **Duplicate defaults**: bot.py DEFAULT_CONTROL vs api/api_server.py + control_defaults.py drift.
- **Context watcher + cleanup**: Both LLM calls using autonomy provider; cleanup caps help but still LLM can hallucinate bad ops.
- **Corrupt recovery**: Good in places (backup + reset), inconsistent in others (autonomy goals can be wiped to {}).
- **Hard-coded secrets in defaults**: DEFAULT_OWNER_IDS (security + maintainability).
- **Pi brain transition**: Ongoing (progress.md) but current main still uses Python custom loop. Side effects: node crashes polluting workspace.
- Tests: Many good targeted tests (tool calls, corrupt writes, rem loop, autonomy engine, etc.). `pytest -q` currently passes.

### 6. Concurrency
- Custom `asyncio.Condition` AI slot with priority (user > background). (bot.py:1661-2000+)
- Per-channel locks for some ops.
- REM/Autonomy/Intel/ContextCleanup have "if previous still running, skip" guards (good).
- But long LLM calls in one slot can delay others; no per-user rate beyond Discord.
- Autonomy tick lock separate.

## Data / Runtime Artifacts Observed
- Many `core.*` ELF core dumps (Node, recent timestamps).
- `data/*.json` + `rem_events.json`, `llm_traces.json`, `autonomy_log.json` etc.
- `__pycache__` for pi_bridge (source may be gone or on branch).
- Branches: experimental-pi-port, pi-harness-testing.

## Recommendations for Increased Reliability (Prioritized)
1. **Tool calling overhaul (biggest win for "leaks" + logic)**: Support native tool_calls in a parallel path, or switch to JSON-only tool plan + separate final answer. Reduce reliance on 200+ lines of brittle regex + strips. Update prompt and _process loop accordingly. Keep XML for models that only do text.
2. **Inter-process coordination**: Add `fcntl.flock` (or a small lock file + helper) around read-modify-write for bot_commands.json, autonomy files, rem state. Or serialize commands better. Make bot do "reload + merge by id" on writes.
3. **Autonomy hardening** (implement the patches from subagents/autonomy.md):
   - Track planned channels in-tick + recheck cooldowns.
   - Re-check bot controls (tools, channels, enabled) in plan/validate/exec.
   - Classify tool returns starting with "Error".
   - Fix backoff logic.
   - Use filtered shared context.
   - Bound intervals.
   - Disable dangerous tools (shell etc.) for autonomy or gate strictly.
4. **Shutdown & lifecycle**:
   - Catch `asyncio.CancelledError` + `BaseException` in REM paths and force-clear `running` state + persist.
   - Central task registry; cancel/await more before close().
   - Add signal handlers for SIGTERM/SIGINT that trigger clean `bot.close()`.
   - Improve PM2 ecosystem (kill_timeout, python from venv, env merge).
5. **Persistence**:
   - Centralize atomic write + add optional `fsync_dir`.
   - Make CreateSiteTool atomic for the HTML file.
   - Consistent "backup corrupt, never silently empty important state".
6. **Hygiene**:
   - Remove/hide hard-coded owner IDs.
   - Unify control defaults.
   - More defensive final output sanitization or model "visible only" turn.
   - Monitor for and clean core dumps; stabilize or isolate Pi/Node usage.
   - Add metrics/visibility for leak strip events, dropped commands, autonomy failures.
7. **Longer term**: Consider extracting "brain" orchestration from bot.py. The Pi effort was attempting this. Or use LiteLLM + instructor/structured outputs.

## Current Mitigations That Are Working
- Atomic writes + corrupt backup in many paths.
- AI slot priority + "previous still running" skips.
- Rich tests for tool parsing/stripping.
- Watermark start-time fix for autonomy (partial).
- Extensive media/REM/context/LLM tracing.
- Provider retry, cooldown, usage-exhaust handling.

## How to Verify Improvements
- `python -m pytest -q`
- `ruff check . --fix`
- Manual: trigger tool calls that used to leak; autonomy tick with multiple same-channel plans; kill -TERM during REM; concurrent dashboard commands + bot work; corrupt a json and recover.
- Watch for fewer core.* and "leaked" artifacts in logs/replies.
- Dashboard /api/llm/traces and autonomy log for behavior.

**This research draws from direct code reads, subagents/*.md audits, tests, data files, progress.md, core dumps, and runtime structure.**

Next steps: Prioritize and land targeted fixes from the lists above.
