# autonomy.py — Correctness Review

Reviewed: `/root/maxwell/autonomy.py` (full file, 984 lines)
Focus: race conditions, async correctness, silent failures, JSON parsing, Discord API usage.

---

## Critical

### 1. Missing `await` on async `list_shared_context()` — silently discards shared context

**File:** `autonomy.py`, line 424
**Severity:** Critical (silent data loss)

```python
shared = self.bot.memory.list_shared_context() if hasattr(self.bot.memory, "list_shared_context") else []
```

`memory.list_shared_context()` is defined as `async def list_shared_context(...)` in `memory.py:469`. Calling it without `await` returns a **coroutine object**, not a list. The coroutine is truthy, so `if shared:` passes, but `shared[:20]` raises `TypeError: unhashable type: 'slice'` on a coroutine. This exception is silently swallowed by the surrounding `except Exception: pass` block.

**Impact:** The entire "SHARED CONTEXT" section is **never included** in the autonomy context. The LLM never sees shared context facts. No error is logged.

**Fix:** Add `await`:
```python
shared = await self.bot.memory.list_shared_context() if hasattr(self.bot.memory, "list_shared_context") else []
```

---

### 2. TOCTOU race in `_log_tick` — concurrent state counter updates can lose increments

**File:** `autonomy.py`, lines 932–943
**Severity:** Critical (data corruption under concurrency)

```python
state = await self.store.load_state()          # lock acquired, read, lock released
# ─── window: another writer can change state here ───
await self.store.patch_state({                   # lock acquired, read again, merge, write, lock released
    "actions_executed_total": state.get("actions_executed_total", 0) + total_exec,
    "actions_failed_total": state.get("actions_failed_total", 0) + total_fail,
    ...
})
```

`load_state()` and `patch_state()` each acquire/release the store lock independently. Between them, `record_error` (called from the exception handler in `_loop`) can write to the same file, causing a lost update when the stale `state` dict's counters are merged back.

While the engine's `_lock` prevents concurrent ticks, the `record_error` call runs *after* the tick lock is released (in the `_loop` exception handler, line 273). A subsequent tick's `_log_tick` can race with `record_error`'s `patch_state` call.

**Impact:** Cumulative `actions_executed_total` and `actions_failed_total` counters can silently lose increments.

**Fix:** Combine the read and update into a single locked operation by adding an `update_state` method that reads and writes under one lock acquisition:
```python
async def update_state(self, fn) -> dict:
    async with self._lock:
        state = _load_json_safe(self.state_file, dict)
        if not isinstance(state, dict):
            state = {}
        fn(state)  # mutate in-place
        await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)
        return state
```
Then in `_log_tick`:
```python
await self.store.update_state(lambda s: s.update({
    "last_tick": _utcnow_iso(),
    "actions_executed_total": s.get("actions_executed_total", 0) + total_exec,
    ...
}))
```

---

## High

### 3. `_atomic_json_write_sync` — file descriptor leak when `os.fdopen()` fails

**File:** `autonomy.py`, lines 51–60
**Severity:** High (resource leak)

```python
def _atomic_json_write_sync(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            ...
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
```

If `os.fdopen(fd, ...)` raises (e.g., `MemoryError`, `OSError`), the raw file descriptor `fd` is **leaked** — it was opened by `mkstemp` but never closed. The `finally` block only unlinks the temp file, not the fd. Repeated failures exhaust the OS fd limit.

**Fix:** Close `fd` explicitly if `fdopen` fails:
```python
fd, tmp = tempfile.mkstemp(...)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        ...
    fd = -1  # fdopen took ownership
    os.replace(tmp, path)
finally:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    if os.path.exists(tmp):
        os.unlink(tmp)
```

---

### 4. Synchronous file I/O under `asyncio.Lock` — blocks the event loop

**File:** `autonomy.py`, lines 126–129 (and 145, 164, 197, 207, 213)
**Severity:** High (event loop stall)

Every `load_state()`, `load_goals()`, and `load_log()` call invokes `_load_json_safe()` which does **synchronous** `path.read_text()` and `json.loads()` while holding an `asyncio.Lock`. This blocks the entire event loop for the duration of the file read.

```python
async def load_state(self) -> dict:
    async with self._lock:        # holds asyncio lock
        data = _load_json_safe(self.state_file, dict)  # BLOCKING I/O
        return data if isinstance(data, dict) else {}
```

The write path correctly uses `await asyncio.to_thread(...)`, but the read path does not.

**Impact:** If the data directory is on a slow filesystem (NFS, cloud disk), every tick can stall the event loop for tens of milliseconds per file read. Multiple reads per tick (state, goals, log) compound the delay.

**Fix:** Wrap reads in `asyncio.to_thread`:
```python
async def load_state(self) -> dict:
    async with self._lock:
        data = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
        return data if isinstance(data, dict) else {}
```

---

### 5. `_parse_plan` balanced-brace algorithm can extract wrong fragment

**File:** `autonomy.py`, lines 607–617
**Severity:** High (silent wrong behavior)

```python
start_idx = text.find("{")
if start_idx != -1:
    depth = 0
    end_idx = -1
    for i in range(start_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
```

If the LLM outputs a thinking block before the JSON (e.g., `Let me think... {"thinking": "hmm"} ... then here is the plan: {"actions": [...]}`), the algorithm grabs the first `{...}` block — the thinking fragment — not the actual plan. This fails JSON parse and produces a silent `do_nothing`.

More critically, if the LLM includes a code example with braces in its preamble (e.g., `Use format {"key": "value"}`), the brace counter extracts that as the JSON.

**Impact:** Valid plans are silently discarded; autonomy does nothing even when the LLM produced a valid plan.

**Fix:** After the brace-matching fallback, try each `{...}` candidate and prefer the one that parses as a dict with an `"actions"` key:
```python
# In the fallback section, collect all balanced blocks and test each
candidates = []
i = 0
while i < len(text):
    if text[i] == "{":
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{": depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[i:j+1])
                    i = j
                    break
    i += 1
# Try each candidate, prefer ones with "actions" key
for c in candidates:
    try:
        obj = json.loads(c)
        if isinstance(obj, dict) and "actions" in obj:
            json_str = c
            break
    except json.JSONDecodeError:
        pass
if json_str is None and candidates:
    json_str = candidates[0]  # fallback to first
```

---

## Medium

### 6. `SyntheticMessage` missing attributes — tools may crash on `AttributeError`

**File:** `autonomy.py`, lines 83–105
**Severity:** Medium (runtime tool failures)

`SyntheticMessage` only provides: `channel`, `author`, `guild`, `content`, `id`, `attachments`, `embeds`, `reference`. But multiple tools access additional message attributes:

- `message.mentions` — used in `bot.py:1465,2216` and several tools that check `self.user in message.mentions`
- `message.guild` used with `message.guild.search()` (SearchMessagesTool), `message.guild.emojis` (ReactTool), `message.guild.me.edit()` (SetNicknameTool)
- `message.author.display_name`, `message.author.id` — used in forward_message admin check (`bot_tools.py:831`)
- `message.type` — may be checked by some tools

When `run_tool` executes a tool with a `SyntheticMessage`, the tool may access `syn_msg.mentions` and get `AttributeError`.

**Fix:** Add missing attributes:
```python
class SyntheticMessage:
    def __init__(self, channel, author, guild, content: str):
        ...
        self.mentions = []
        self.role_mentions = []
        self.flags = discord.MessageFlags()
        self.type = discord.MessageType.default
        self.pinned = False
        self.tts = False
```

---

### 7. `record_error` in `_loop` can mask the original exception

**File:** `autonomy.py`, lines 272–275
**Severity:** Medium (debugging difficulty)

```python
except Exception as e:
    logger.error(f"AutonomyEngine tick error: {e}")
    try:
        await self.store.record_error(str(e))
    except Exception:
        logger.error("Failed to record autonomy error to store")
```

If `record_error` itself raises (e.g., disk full, corrupted state), the second `except` catches that new exception but only logs a generic message. The original error context is preserved in the first `logger.error`, but the `record_error` failure could indicate a systemic issue (full disk, permission loss) that affects all subsequent ticks too.

More importantly, `record_error` calls `patch_state` which does synchronous JSON parsing under the store lock. If the state file is corrupted, `_load_json_safe` will return defaults, overwriting the actual state. The error gets recorded, but other state data is silently reset.

**Fix:** Log both exceptions with context:
```python
except Exception as e:
    logger.error(f"AutonomyEngine tick error: {e}", exc_info=True)
    try:
        await self.store.record_error(str(e))
    except Exception as rec_err:
        logger.error(f"Failed to record autonomy error to store: {rec_err} (original: {e})")
```

---

### 8. `_exec_send_dm` — no handling for DM-disabled users or guild members

**File:** `autonomy.py`, lines 789–808
**Severity:** Medium (unhandled exception path)

```python
dm_channel = None
for ch in getattr(self.bot, "private_channels", []):
    if isinstance(ch, discord.DMChannel):
        recipient = getattr(ch, "recipient", None)
        if recipient and str(recipient.id) == str(user_id):
            dm_channel = ch
            break
if dm_channel is None:
    dm_channel = await user.create_dm()

await dm_channel.send(content)
```

If the target user has DMs disabled (common for users in servers with privacy settings), `dm_channel.send()` raises `discord.Forbidden` (HTTP 403). This exception bubbles up to the outer `execute()` handler which catches it, but the error message is generic: `"Cannot send messages to this user"`.

There's also a race: `private_channels` is an in-memory cache that can change between the iteration and `create_dm()`. If the bot's `private_channels` list is mutated during iteration (e.g., a new DM arrives), this could raise `RuntimeError: dictionary changed size during iteration`.

**Fix:** Wrap `dm_channel.send()` in a specific try/except:
```python
try:
    await dm_channel.send(content)
except discord.Forbidden:
    result["result"] = "error"
    result["error"] = "user has DMs disabled or blocked the bot"
    return
except discord.HTTPException as e:
    result["result"] = "error"
    result["error"] = f"Discord API error: {e}"
    return
```

---

### 9. `run_tool` silently swallows channel resolution errors

**File:** `autonomy.py`, lines 850–855
**Severity:** Medium (silent failures)

```python
if target_cid:
    try:
        channel = self.bot.get_channel(int(target_cid))
        if channel is None:
            channel = await self.bot.fetch_channel(int(target_cid))
    except (ValueError, TypeError, discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
```

If the user specifies a `channel_id` in `tool_args` but the channel is not found (e.g., deleted, wrong ID), the error is silently swallowed. The code falls through to try `auto_channels`, which may succeed with a **different channel** than intended. The tool then executes in the wrong channel.

**Fix:** Log the fallback and/or fail explicitly if a user-specified channel was not found:
```python
if target_cid:
    try:
        channel = self.bot.get_channel(int(target_cid))
        if channel is None:
            channel = await self.bot.fetch_channel(int(target_cid))
    except (ValueError, TypeError):
        logger.warning(f"run_tool: invalid channel_id {target_cid!r}")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning(f"run_tool: channel {target_cid} not accessible: {e}")
```

---

### 10. `_log_tick` — `load_state` and `patch_state` are separate lock acquisitions

**File:** `autonomy.py`, lines 932–943
**Severity:** Medium (see also #2 above for the race detail)

Even outside the race with `record_error`, the two separate lock acquisitions mean the store lock is held, released, then re-acquired. This wastes time and creates unnecessary contention. The comment "avoids triple file read" is misleading — it actually does two file reads (one in `load_state`, one in `patch_state`).

**Fix:** See fix for #2 — use a single `update_state` call.

---

## Low

### 11. `_parse_plan` code-fence regex uses greedy `.*` — may over-match

**File:** `autonomy.py`, line 604
**Severity:** Low

```python
m = re.search(r"```(?:json)?\s*\n?(\{.*\})\s*```", text, re.DOTALL)
```

With `re.DOTALL`, `.*` matches everything including newlines, and is **greedy**. If the LLM outputs multiple code fences, this regex matches from the first `{` in the first fence to the last `}` in the last fence, potentially capturing garbage text between them.

Example LLM output:
```
```json
{"example": true}
```
Some text
```json
{"thought": "...", "actions": [...]}
```
```

The regex would match `{"example": true}\n```\nSome text\n```json\n{"thought": "...", "actions": [...]}`  which fails JSON parse.

**Fix:** Use a non-greedy match or match only the content between a single fence pair:
```python
m = re.search(r"```(?:json)?\s*\n?(\{[^`]*)\s*```", text, re.DOTALL)
```
Or better, use non-greedy: `r"```(?:json)?\s*\n?(\{.*?\})\s*```"` (but this fails if the JSON itself contains `}` then more content).

---

### 12. `SyntheticMessage.id = 0` — may cause issues with tools that use message ID

**File:** `autonomy.py`, line 93
**Severity:** Low

```python
self.id = 0
```

Discord message IDs are snowflakes (18+ digit integers). An ID of `0` is invalid. Tools like `DeleteMessageTool` (line 554) do `message.channel.fetch_message(int(message_id))`, so a `0` ID passed as context could cause a `NotFound` error if any tool tries to reference the synthetic message's ID.

This is unlikely in practice since tools typically use explicitly-provided IDs, not the context message's ID.

**Fix:** Use a clearly-invalid sentinel or `None`:
```python
self.id = None  # or a fake snowflake like 1
```

---

### 13. `_exec_run_tool` `exec_kwargs` may include unexpected keys from LLM

**File:** `autonomy.py`, line 887
**Severity:** Low

```python
exec_kwargs = {k: v for k, v in tool_args.items() if k not in {"channel_id"}}
```

Only `channel_id` is excluded. If the LLM passes extra keys (e.g., `"reason"`, `"kind"`, or hallucinated parameters), they're forwarded to the tool as `**kwargs`. Most tools use `**kwargs` and ignore unknown keys, but some may behave unexpectedly.

**Fix:** Also exclude `"content"`, `"prompt"`, `"reason"`, `"kind"`, and other autonomy-level keys:
```python
_META_KEYS = {"channel_id", "content", "prompt", "reason", "kind", "target_channel_id"}
exec_kwargs = {k: v for k, v in tool_args.items() if k not in _META_KEYS}
```

---

### 14. No tests exist for `autonomy.py`

**File:** N/A (project-wide)
**Severity:** Low (maintainability risk)

No test files matching `test_autonomy*` exist. The `_parse_plan` method alone has multiple code paths (pure JSON, code fence, balanced brace, malformed input, missing actions key, non-list actions, validation of each action kind). None of these are tested.

**Fix:** Add `tests/test_autonomy.py` with at least:
- `_parse_plan` with valid JSON, code-fenced JSON, malformed input, missing keys
- Validation of each action kind (send_dm with missing uid, post_channel with empty content, etc.)
- `SyntheticMessage` attribute access smoke test

---

## Summary

| # | Issue | Severity | Line(s) | Category |
|---|-------|----------|---------|----------|
| 1 | Missing `await` on `list_shared_context()` | **Critical** | 424 | Async correctness |
| 2 | TOCTOU race on state counters in `_log_tick` | **Critical** | 932–943 | State consistency |
| 3 | FD leak in `_atomic_json_write_sync` | **High** | 51–60 | Resource leak |
| 4 | Sync file I/O under `asyncio.Lock` | **High** | 126, 145, 164, 197, 207, 213 | Async correctness |
| 5 | Balanced-brace parser extracts wrong fragment | **High** | 607–617 | JSON parsing |
| 6 | `SyntheticMessage` missing attributes | **Medium** | 83–105 | Discord API |
| 7 | `record_error` can mask original exception | **Medium** | 272–275 | Error handling |
| 8 | DM send unhandled `Forbidden` / race on `private_channels` | **Medium** | 789–808 | Discord API |
| 9 | Silent channel fallback in `run_tool` | **Medium** | 850–855 | Silent failure |
| 10 | Double lock acquisition in `_log_tick` | **Medium** | 932–943 | State consistency |
| 11 | Greedy regex in code-fence extraction | **Low** | 604 | JSON parsing |
| 12 | `SyntheticMessage.id = 0` invalid snowflake | **Low** | 93 | Discord API |
| 13 | Unfiltered kwargs forwarded to tool | **Low** | 887 | Error handling |
| 14 | No test coverage for autonomy module | **Low** | — | Testing |
