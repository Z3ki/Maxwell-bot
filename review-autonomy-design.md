# Review: autonomy.py — Design Issues & Findings

**File:** `/root/maxwell/autonomy.py` (853 lines)  
**Date:** 2026-05-30  
**Scope:** Design issues, dead code, performance problems, maintainability concerns

---

## 1. Context Gathering (`gather_context()`)

### 1.1 Redundant data fetches — goals loaded twice per tick

- **Severity:** Low
- **Location:** `gather_context()` lines ~346–358 AND `plan()` lines ~500–510
- **Issue:** `self.store.load_goals()` is called once in `gather_context()` (to build the ACTIVE GOALS section) and again in `plan()` (to build the goals_text for the prompt). The second call re-reads the same JSON file from disk with no change in between.
- **Fix:** Have `gather_context()` return the raw goals alongside the text, or pass goals as a parameter to `plan()`. Alternatively, `plan()` already receives the full context string that includes the goals section — just extract goals from there.

### 1.2 DM history fetches are expensive and potentially rate-limited

- **Severity:** High
- **Location:** lines ~371–388 (DM history section of `gather_context()`)
- **Issue:** Iterates up to 20 DM channels, each issuing a `channel.history(limit=20)` API call. On first run or after a long gap, this fires 20 concurrent-ish HTTP requests to Discord. Discord rate limits are per-route, and history requests for different channels hit different endpoints, but still — this is a burst of up to 20 API calls every autonomy tick (default 300s). With large guilds and many DMs, this can exhaust rate limits.
- **Fix:** Add a concurrency limit (e.g., `asyncio.Semaphore(3)`) or cache DM history with a short TTL (e.g., 60s). Also consider skipping DMs with no recent activity by checking `channel.last_message_id` if available.

### 1.3 Channel history fetches also fire many API calls

- **Severity:** Medium
- **Location:** lines ~396–424 (CHANNEL ACTIVITY section)
- **Issue:** Up to 10 channels, each with `ch.history(limit=10)`. If `get_channel()` returns None (common for channels in guilds the bot hasn't fully loaded), it falls back to `fetch_channel()` — another API call. Worst case: 10 `fetch_channel()` + 10 `history()` = 20 API calls.
- **Fix:** Same as above — semaphore or cache. Also consider using `bot.get_all_channels()` cache first before hitting the API.

### 1.4 `drain_slice(last_tick)` on first run returns everything

- **Severity:** Medium
- **Location:** lines ~334–336
- **Issue:** When `last_tick` is None (first ever tick), `drain_slice(None)` returns ALL events in the buffer (line 171-172 of memory.py). This could be hundreds of events, all injected into the context. The `events[-30:]` slice at line 339 limits output, but the full buffer is still loaded into memory.
- **Fix:** Initialize `last_tick` to `_utcnow_iso()` in the initial state so the first tick only sees new events, or handle the None case explicitly.

### 1.5 Missing useful context: guild list and bot permissions

- **Severity:** Low
- **Location:** `gather_context()` (entire method)
- **Issue:** The LLM has no information about which guilds the bot is in, what channels exist, or what permissions the bot has. This means the LLM might try to post to channels it doesn't have access to, or DM users in guilds it's not part of. The error is caught gracefully in `_exec_*` methods, but it wastes LLM reasoning on impossible actions.
- **Fix:** Add a brief guild/channel listing (names + IDs, truncated) to the context. Even a simple `f"Guilds: {[g.name for g in bot.guilds]}"` would help.

### 1.6 `list_shared_context()` is async but called without await

- **Severity:** Critical (bug)
- **Location:** line ~428
- **Issue:** The code does:
  ```python
  shared = self.bot.memory.list_shared_context() if hasattr(self.bot.memory, "list_shared_context") else []
  ```
  But `list_shared_context` in memory.py (line 469) is an `async def`. Calling it without `await` returns a coroutine object, not the actual list. This means `shared` is always a coroutine (truthy), and `ctx_lines` will iterate over coroutine attributes instead of the actual context lines. This will either produce garbage output or raise an exception silently caught by the outer try/except.
- **Fix:** `shared = await self.bot.memory.list_shared_context()` — add `await`.

---

## 2. LLM Prompt Quality (`plan()`)

### 2.1 Prompt tells LLM to check for missed DMs but provides no sender metadata

- **Severity:** Medium
- **Location:** system prompt (lines ~520–560)
- **Issue:** The prompt says "Check if anyone talked to you while you were asleep and respond if needed." The DM history section provides messages like `[DM:123] Alice: hey`, but there's no user ID in the DM section — just display names. The LLM must then produce `target_user_id` for `send_dm` actions, but it has no ID mapping. It will likely hallucinate IDs or skip DM follow-ups.
- **Fix:** Include user IDs in the DM history lines: `f"[DM:{channel.id}] {author_name} ({m.author.id}): {content}"`.

### 2.2 Prompt mentions "goals" three times with different detail levels

- **Severity:** Low
- **Location:** context sections + prompt body
- **Issue:** Goals appear in: (1) the ACTIVE GOALS context section, (2) the "Your goals:" prompt section, (3) the instructions. The prompt section re-reads goals from the store. This is confusing — the LLM sees goals listed twice. Could cause it to create duplicate goals.
- **Fix:** Remove the separate `goals_text` block from the prompt; rely on the context section only. Or merge them.

### 2.3 No explicit instruction about channel permissions or user relationships

- **Severity:** Medium
- **Location:** system prompt
- **Issue:** The prompt doesn't tell the LLM which channels/users are accessible. The LLM might try to DM someone who has DMs disabled, or post to a channel the bot can't access. The "What you should NOT do" section mentions not spamming but doesn't mention respecting permissions.
- **Fix:** Add a brief note: "Only DM users you can see in the context. Only post to channels listed in your context." Or better yet, provide a list of accessible channel/user IDs.

### 2.4 JSON format example includes `run_tool` with `web_search` — tool may not exist

- **Severity:** Low
- **Location:** system prompt example actions
- **Issue:** The example shows `"tool_name": "web_search"` which may or may not be in `self.bot.tools`. If it's not available, the LLM may try to use it anyway (the validation will drop it, but it wastes an action slot). Worse, the LLM might hallucinate tool names that sound plausible.
- **Fix:** Dynamically generate examples from available tools, or use a generic placeholder like `"tool_name": "<one of the available tools>"`.

### 2.5 `CONTEXT_BUDGET = 8000` may be too small for the prompt itself

- **Severity:** Medium
- **Location:** line 32, and context truncation at line ~442
- **Issue:** The context is truncated to 8000 chars, but the system prompt template (lines ~520–560) adds another ~1500+ chars of instructions + tool descriptions + goals text + recent actions text. Total prompt size could easily exceed 12000 chars. For models with smaller context windows or token-based pricing, this is significant. The truncation also happens mid-sentence which can confuse the LLM.
- **Fix:** Truncate at a natural boundary (e.g., last complete section). Or increase the budget and add token counting instead of char counting. Also consider that the prompt itself should be budgeted separately from the context.

---

## 3. Action Execution

### 3.1 `_exec_send_dm` — `fetch_user()` exceptions not caught

- **Severity:** High
- **Location:** lines ~795–797
- **Issue:** `self.bot.fetch_user(int(user_id))` can raise `discord.NotFound`, `discord.HTTPException`, or `ValueError` (if user_id is malformed). These are not caught, so the exception propagates to the generic handler in `execute()` which marks it as "error" but provides a generic error message. The `user is None` check after `fetch_user` is dead code — `fetch_user` raises rather than returning None.
- **Fix:** Wrap in try/except:
  ```python
  try:
      user = await self.bot.fetch_user(int(user_id))
  except (discord.NotFound, discord.HTTPException, ValueError):
      result["result"] = "error"
      result["error"] = "user not found or API error"
      return
  ```

### 3.2 `_exec_send_dm` — `user.create_dm()` can fail silently

- **Severity:** Medium
- **Location:** lines ~808–810
- **Issue:** `user.create_dm()` can raise `discord.HTTPException` (e.g., user has DMs disabled). This is not caught and will propagate to the generic handler. The error message won't indicate it was a DM creation failure specifically.
- **Fix:** Wrap in try/except with a clear error message: "Failed to create DM channel — user may have DMs disabled."

### 3.3 `_exec_send_dm` — linear scan of `private_channels` is O(n)

- **Severity:** Low
- **Location:** lines ~800–806
- **Issue:** Iterates through all private channels to find a DM with the target user. Discord.py's `private_channels` is a list, not a dict. With many DMs, this is slow. Also, `private_channels` may not be populated if the bot hasn't received a DM from that user yet.
- **Fix:** Not critical since the list is usually small (<100), but could cache DM channel IDs by user ID. The `create_dm()` fallback handles the cache miss correctly.

### 3.4 `_exec_post_channel` — no permission check before `channel.send()`

- **Severity:** Medium
- **Location:** lines ~824–830
- **Issue:** `channel.send()` can raise `discord.Forbidden` if the bot lacks `send_messages` permission. This is caught by the generic handler but the error message is generic. Also, no check for `send_messages_in_threads` for thread channels.
- **Fix:** Add a permission check: `if not channel.permissions_for(channel.guild.me).send_messages:` before attempting to send.

### 3.5 `_exec_run_tool` — tool args passed without deep validation

- **Severity:** Medium
- **Location:** lines ~848–860
- **Issue:** `tool_args` values are passed through as-is (line 856: `safe_args[str(k)] = v`). The LLM could produce nested dicts, lists, or other complex types that tools may not handle. The "sanitize" comment at line 853 is misleading — it only ensures keys are strings, not values.
- **Fix:** Either document that tools must handle arbitrary args, or add value sanitization (coerce to str/int/float/bool/None).

### 3.6 `_exec_run_tool` — SyntheticMessage is a thin duck-type

- **Severity:** Medium
- **Location:** SyntheticMessage class (lines ~85–105) and `_exec_run_tool` usage
- **Issue:** SyntheticMessage only has the bare minimum attributes. Tools that access `message.id`, `message.type`, `message.created_at`, `message.mentions`, `message.role_mentions`, `message.channel_mentions`, `message.flags`, etc. will get AttributeError. The `id=0` is particularly dangerous — some tools may use message ID for deduplication or logging.
- **Fix:** Add more common attributes with sensible defaults: `self.type = discord.MessageType.default`, `self.created_at = datetime.now(timezone.utc)`, `self.mentions = []`, etc. Or document which tools are safe to use with SyntheticMessage.

### 3.7 `_exec_run_tool` — fallback to `_auto_channels` may surprise the LLM

- **Severity:** Low
- **Location:** lines ~862–870
- **Issue:** If no channel is specified in the action, the method falls back to the first auto_channel. The LLM has no way to know which channel this will be, and the tool result summary doesn't indicate which channel was used. This makes debugging hard and could cause the LLM to post to unintended channels.
- **Fix:** Log the fallback channel selection. Consider requiring the LLM to specify a channel for tool actions.

### 3.8 No action-level timeout

- **Severity:** High
- **Location:** `execute()` method (lines ~750–790)
- **Issue:** Individual actions have no timeout. If `channel.send()` hangs (network issue), `fetch_user()` hangs, or a tool execution hangs, the entire tick blocks indefinitely. The tick lock (`self._lock`) prevents concurrent ticks, so one hung action blocks all future autonomy.
- **Fix:** Wrap each action in `asyncio.wait_for(..., timeout=30)` or similar. The tick itself has no outer timeout either.

### 3.9 Rate limiting: multiple DMs/posts sent without delay

- **Severity:** Medium
- **Location:** `execute()` loop (lines ~750–790)
- **Issue:** If the LLM produces 5 `send_dm` actions, they're all sent sequentially without any inter-message delay. Discord rate limits can kick in, especially for DMs (which have stricter limits). The first few may succeed, but later ones will hit 429s.
- **Fix:** Add a small delay between actions (e.g., `await asyncio.sleep(1)`) or check rate limit headers from previous responses.

---

## 4. Logging & Debugging

### 4.1 Raw LLM response not logged

- **Severity:** Medium
- **Location:** `plan()` method (lines ~555–575)
- **Issue:** The raw LLM response is only logged if JSON parsing fails (in `_parse_plan` warnings). On successful parse, the raw response is discarded. This makes it impossible to debug cases where the LLM produces valid JSON but semantically wrong actions (e.g., DMing the wrong person).
- **Fix:** Log `raw_response[:500]` at DEBUG level after receiving it, regardless of parse outcome.

### 4.2 No context size logging

- **Severity:** Low
- **Location:** `gather_context()` return (line ~442)
- **Issue:** The context string is truncated to CONTEXT_BUDGET chars, but there's no log of the original size vs. truncated size. If the context is consistently being truncated, important information is being silently dropped.
- **Fix:** Log at DEBUG: `f"Context gathered: {len(full)} chars ({len(full) - CONTEXT_BUDGET} truncated)"` if truncation occurred.

### 4.3 `_exec_run_tool` logs tool_args but not tool result

- **Severity:** Low
- **Location:** lines ~877–885
- **Issue:** The result summary is logged in the action log entry, but the full tool output is only captured in `result["content_summary"]` (truncated to 300 chars). For tools that produce structured output (JSON, etc.), 300 chars may not be enough to diagnose issues.
- **Fix:** Log tool results at DEBUG level with more chars (e.g., 1000).

### 4.4 Error recording truncates to 2000 chars

- **Severity:** Low
- **Location:** `record_error()` (line ~214), and multiple `str(e)[:2000]` truncations
- **Issue:** Error strings are truncated to 2000 chars. For long stack traces (e.g., from tool execution), this loses the root cause. The truncation is also inconsistent — some places use `[:1000]`, others `[:2000]`.
- **Fix:** Standardize truncation limits. For errors, log the full exception at ERROR level and only truncate for state storage.

### 4.5 REM event recording uses `logger.debug` for failures

- **Severity:** Low
- **Location:** lines ~780–782
- **Issue:** If recording an autonomy action to the REM event log fails, it's logged at DEBUG level. This means production deployments (typically INFO level) will silently lose these events. Since REM depends on these events for memory assimilation, this is a silent data loss.
- **Fix:** Use `logger.warning` instead of `logger.debug`.

---

## 5. Memory & Resource Leaks

### 5.1 `_log_tick` has TOCTOU race on state counters

- **Severity:** Medium
- **Location:** lines ~700–715
- **Issue:** `load_state()` reads the current state, then `patch_state()` reads it again and updates. If another process (e.g., API server updating `last_error`) writes to the state file between `load_state()` and `patch_state()`, the counter increments from the first read are based on stale data. The `patch_state` method does read-modify-write under its own lock, but the `load_state` call at line 702 is outside that lock.
- **Fix:** Use a single `patch_state` call with a lambda/increment function, or move the counter increment inside the lock. Since `patch_state` already does read-modify-write, just pass the delta:
  ```python
  # Instead of load_state + manual increment, use patch_state with the deltas
  # and compute counters inside patch_state's lock.
  ```
  Actually, the simplest fix: just don't read state separately. The `patch_state` at line 704 already reads the current state. Just pass the deltas and let the store handle the increment in its locked section. But `patch_state` currently does `state.update(updates)` which would overwrite, not increment. So either add an `increment_state(key, delta)` method, or accept the race as low-probability.

### 5.2 Goals grow unboundedly — no limit on goal count

- **Severity:** Medium
- **Location:** `add_goal()` (lines ~172–184)
- **Issue:** Every `create_goal` action appends to the goals list. There's no cap on the number of goals. Over many ticks, the LLM could create hundreds of goals (especially if it's not carefully checking existing goals). The goals file grows, and `load_goals()` + `load_context()` both read it every tick.
- **Fix:** Add a max goals limit (e.g., 50) and reject new goals when the limit is reached. Or add goal deduplication (check if a similar goal already exists).

### 5.3 Action log ring buffer is correct but log entries accumulate metadata

- **Severity:** Low
- **Location:** `_log_tick()` (lines ~720–740)
- **Issue:** Each log entry includes `tool_args` (dict), `thought` (up to 1000 chars), and other metadata. With LOG_RING_SIZE=200, the log file could grow to several MB. Not a leak per se, but the file is re-read every tick via `load_log()`.
- **Fix:** Consider reducing `thought` truncation to 200 chars in log entries, or omitting `tool_args` from log entries (they're rarely useful for debugging past actions).

### 5.4 `SyntheticMessage` objects are not cleaned up

- **Severity:** Low
- **Location:** `_exec_run_tool()` (lines ~870–875)
- **Issue:** Each tool execution creates a SyntheticMessage. These are lightweight objects (no resources to close), so this isn't a real leak. However, if tools store references to the message (e.g., in a cache), the SyntheticMessage with `id=0` could cause deduplication issues.
- **Fix:** Not critical. Just be aware that `id=0` is shared across all SyntheticMessages and could cause collisions in any dedup logic.

### 5.5 No graceful handling of bot disconnection during tick

- **Severity:** Medium
- **Location:** `execute()` method
- **Issue:** If the bot disconnects from Discord mid-tick (e.g., WebSocket drops), `channel.send()` and `fetch_user()` will raise `discord.HTTPException` or `ConnectionClosed`. The generic error handler catches these, but the tick continues trying remaining actions (which will all fail). This wastes time and generates noise in logs.
- **Fix:** Check `self.bot.is_closed()` at the start of each action in the execute loop. If the bot is disconnected, abort remaining actions.

### 5.6 `_loop` catches all exceptions but doesn't back off on repeated failures

- **Severity:** Medium
- **Location:** `_loop()` (lines ~250–270)
- **Issue:** If `tick()` consistently fails (e.g., AI provider is down), the loop catches the error and sleeps for the normal interval (default 300s). There's no exponential backoff. If the interval is short (e.g., 30s minimum), this means repeated failed LLM calls every 30s, each consuming an AI slot and generating error logs.
- **Fix:** Add backoff on consecutive failures: track `consecutive_failures` in state, and multiply the sleep interval by `min(2^consecutive_failures, 10)` (capped at 10x). Reset on success.

---

## 6. Dead Code & Minor Issues

### 6.1 `tempfile` and `os` imports used only by `_atomic_json_write_sync`

- **Severity:** Low (code hygiene)
- **Location:** imports at top (lines ~11–12)
- **Issue:** `tempfile` and `os` are imported for `_atomic_json_write_sync`, which duplicates the same function from `memory.py` and `rem.py`. This is the third copy of this pattern in the codebase.
- **Fix:** Import from `memory` directly: `from memory import _atomic_json_write_sync` (same as `rem.py` does).

### 6.2 `_load_json_safe` duplicates `memory.py` pattern

- **Severity:** Low (code hygiene)
- **Location:** lines ~43–53
- **Issue:** `_load_json_safe` is a copy of the pattern used in memory.py/rem.py. Same function, same logic, different name.
- **Fix:** Consolidate into a shared utility module or import from memory.py.

### 6.3 `AutonomyStore._lock` is per-instance but state/goals/log share it

- **Severity:** Low
- **Location:** `AutonomyStore` class (lines ~110–215)
- **Issue:** All three data files (state, goals, log) share a single lock. This means a slow log write blocks state reads. Not a correctness issue but could cause unnecessary contention.
- **Fix:** Use separate locks per file, or accept the simplicity trade-off (current approach is fine for low-frequency operations).

### 6.4 `start()` is not truly idempotent if `_task` is done

- **Severity:** Low
- **Location:** `start()` (lines ~228–233)
- **Issue:** If `_task` exists but is done (crashed), `start()` returns early without restarting. The condition `self._task is not None and not self._task.done()` correctly handles this — wait, actually it does handle it: if `_task.done()` is True, the condition is False, so it proceeds to create a new task. This is correct. No issue here.
- **Verdict:** False alarm — this is correctly implemented.

### 6.5 `_last_thought` is not thread-safe

- **Severity:** Low
- **Location:** `self._last_thought` used in `tick()` and `_log_tick()`
- **Issue:** `_last_thought` is set in `_parse_plan()` (line ~660) and read in `_log_tick()` (line ~700). Both run in the same async context (under `self._lock`), so there's no race. This is fine.
- **Verdict:** No issue.

---

## Summary Table

| # | Issue | Severity | Location | Category |
|---|-------|----------|----------|----------|
| 1.1 | Goals loaded twice per tick | Low | gather_context + plan | Redundant fetch |
| 1.2 | DM history: up to 20 API calls per tick | High | gather_context L371-388 | Performance |
| 1.3 | Channel history: up to 20 API calls per tick | Medium | gather_context L396-424 | Performance |
| 1.4 | First tick loads entire event buffer | Medium | gather_context L334-336 | Edge case |
| 1.5 | No guild/permission context for LLM | Low | gather_context | Missing context |
| 1.6 | `list_shared_context()` called without await | **Critical** | gather_context L428 | Bug |
| 2.1 | DM history has no user IDs | Medium | plan() prompt | Prompt quality |
| 2.2 | Goals shown twice to LLM | Low | plan() prompt | Prompt quality |
| 2.3 | No permission/access info in prompt | Medium | plan() prompt | Prompt quality |
| 2.4 | Example tool may not exist | Low | plan() prompt | Prompt quality |
| 2.5 | Context budget may be too small | Medium | CONTEXT_BUDGET | Config |
| 3.1 | `fetch_user()` exceptions not caught | High | _exec_send_dm L795 | Robustness |
| 3.2 | `create_dm()` failure not caught | Medium | _exec_send_dm L808 | Robustness |
| 3.3 | Linear scan of private_channels | Low | _exec_send_dm L800 | Performance |
| 3.4 | No permission check before send | Medium | _exec_post_channel | Robustness |
| 3.5 | Tool args not deeply validated | Medium | _exec_run_tool | Robustness |
| 3.6 | SyntheticMessage too thin for many tools | Medium | SyntheticMessage | Design |
| 3.7 | Fallback to auto_channels is opaque | Low | _exec_run_tool | Debuggability |
| 3.8 | No action-level timeout | High | execute() | Reliability |
| 3.9 | No delay between rapid-fire actions | Medium | execute() | Rate limiting |
| 4.1 | Raw LLM response not logged | Medium | plan() | Debuggability |
| 4.2 | No context size logging | Low | gather_context | Debuggability |
| 4.3 | Tool results truncated in logs | Low | _exec_run_tool | Debuggability |
| 4.4 | Error truncation inconsistent | Low | multiple | Debuggability |
| 4.5 | REM recording failures logged at DEBUG | Low | execute() | Silent data loss |
| 5.1 | TOCTOU race on state counters | Medium | _log_tick | Race condition |
| 5.2 | Goals grow unboundedly | Medium | add_goal | Resource leak |
| 5.3 | Log entries accumulate metadata | Low | _log_tick | Resource usage |
| 5.4 | SyntheticMessage id=0 shared | Low | _exec_run_tool | Design |
| 5.5 | No disconnection handling mid-tick | Medium | execute() | Reliability |
| 5.6 | No backoff on repeated failures | Medium | _loop | Reliability |
| 6.1 | Duplicate atomic_json_write_sync | Low | imports | Code hygiene |
| 6.2 | Duplicate _load_json_safe | Low | L43-53 | Code hygiene |
| 6.3 | Single lock for all store files | Low | AutonomyStore | Contention |

---

## Priority Recommendations

1. **Fix the `await` bug** (1.6) — this is a real runtime error.
2. **Add action-level timeouts** (3.8) — prevents the entire autonomy system from hanging.
3. **Catch `fetch_user` exceptions** (3.1) — prevents crash on invalid user IDs.
4. **Add DM history rate limiting** (1.2) — prevents Discord API rate limit violations.
5. **Include user IDs in DM context** (2.1) — enables the LLM to actually follow up on DMs.
6. **Add backoff on repeated failures** (5.6) — prevents cascading failures.
7. **Log raw LLM responses at DEBUG** (4.1) — essential for debugging prompt issues.
