# Second-opinion review of `fix/codebase-audit`

Scope: commit `bea09e2` ("Fix 50+ defects found in a full-codebase audit") plus
`AUDIT_REPORT.md` (`4d81f51`), reviewed hunk by hunk against `origin/dev`, with
spot checks of the surrounding code. My fixes are in the "Review fixes" commit on
top of the branch.

## Result

- The first pass is mostly sound, but it shipped **one serious regression**
  (typing cleanup raised `CancelledError` on every normal release) and three
  fixes that were incomplete or overreached (promise negation, truncated tool
  call detection, watermark race). One fix replaced a crash with a dangerous
  silent fallback (shard ids). All of these are fixed and pinned by tests.
- `python3 -m pytest tests/unit -q`: all green (first-pass tests plus 13 new
  ones in `tests/unit/test_review_fixes_unit.py`).
- `python3 -m ruff check .`: clean. `ruff format --check tests/unit`: clean.
- Every new test except the shell reaper test was confirmed to **fail on the
  first-pass code** and pass after my fix. The shell reaper test hangs on
  `origin/dev` code (the spin it pins), which I confirmed with `timeout 20`.
- Related legacy tests (`tests/test_typing_status.py`, `test_message_pipeline.py`,
  `test_streaming*.py`, `test_custom_tool_calls.py`, `test_providers.py`,
  `test_runtime_lifecycle.py`, `test_inbound_reliability.py`,
  `test_tool_progress.py`, and others) pass. Four modules
  still fail to collect because the host `discord` package has no `discord.ui`.
  That failure existed before this branch.

## What I fixed

1. **`typing_status.py`: regression introduced by the first pass (severe).**
   The audit removed `asyncio.CancelledError` from the suppress list in
   `ChannelTyping._close` to "propagate cancellation". discord.py's
   `Typing.__aexit__` cancels `cm.task`, and `_close` then cancels and awaits that
   same task, so `await task` raises `CancelledError` on **every** normal
   release. Each `release()` / `suspend()` then raised into the reply path and
   aborted it. The unit tests missed this because no test used a context
   manager with a real task. The fix re-raises only when the current task is
   itself being cancelled (`current_task().cancelling()`). In all other cases it
   swallows the expected cancellation of the child task. Tests cover both a
   normal release and a cancellation of the caller during release.

2. **`maxwell_core/tools/results.py`: the negation filter overreached.** The
   first pass skipped any clause that contained a negation word anywhere. Common
   promises then stopped looping back, so the bot announced work it never did:
   "lemme check why it's not loading" and "I'll fix the thing that isn't working"
   both read as refusals. That failure is worse than the extra turn it was meant
   to prevent. Now a negation counts only when it falls within the 3 words
   before the promise phrase ("I'm not going to create", "I really don't think
   I'll make it"). The first-pass refusal tests still pass.

3. **`maxwell_core/providers/custom_tools.py`: `truncated_tool_call` missed
   common truncations.** It relied on `has_pending_json` (the last `{` comes
   after the last `}`). A call cut off after its nested `arguments` object
   closed, such as `{"name": "shell", "arguments": {"command": "ls"}`, passed
   that check, and the raw JSON was posted as the reply. The property now runs
   a real balance check (`_find_balanced_json_end`) on the held opener.

4. **`message_pipeline.Watermarks`: the race fix was incomplete.** The lock
   fixed the "dict changed size" error but kept a lost update. A `note()` that
   lands while `save()` writes the file sets `_dirty=True`, and `save()` then
   sets `_dirty=False`, so the newer mark is not saved until some later note. I
   added a version counter (dirty is cleared only if nothing changed during the
   write). I also added a save lock so the periodic `to_thread` save and the
   synchronous shutdown save in `bot.py` never write the shared `.tmp` file at
   the same time.

5. **`runtime_lifecycle.shard_launch_kwargs`: a crash became a silent shard
   duplication.** Dropping out-of-range `SHARD_IDS` is fine for a stray typo.
   When **all** ids are out of range, the first pass returned only
   `shard_count`, and discord.py then runs **every** shard in this process. In a
   multi-process deployment that duplicates every other process's shards,
   which means double replies. That case now raises a clear `ValueError`, and a
   partial drop logs a warning instead of being silent.

6. **`bot_parts/recovery._abandon_offline_inbound`: the batched path swallowed
   errors.** `contextlib.suppress(Exception)` plus `return 0` meant a locked or
   failed journal silently skipped stale-row suppression, and those rows could
   be replayed later. The method now logs the failure and falls back to the
   existing per-row sweep.

7. **`plugins/maxwell_extras/privacy_ui.py`: wrong user-facing claim and
   imports out of order.** The new error message said "your storage setting
   could not be saved, so nothing was removed" for any exception.
   `delete_live_data` can also fail after the setting is saved, for example in
   the job cancel, the Last.fm step, or `delete_data`, so the message could
   tell a user that nothing was deleted when some data was. The message now
   says the deletion did not finish and some data may still be stored, and
   asks the user to retry (the operation is idempotent). I also moved the
   `logger =` assignment out of the middle of the import block.

8. **`providers.py`: cosmetic.** The text-only fallback log line passed a
   meaningless `%s` argument (`"[provider body omitted]"`). I removed it.

## Verified as correct (agree with the first pass)

- **Hydration:** the `_is_newer_than_live` snowflake ordering is correct.
  Memory rows always carry `message_id` (a uuid hex when there is no
  snowflake), so skipping empty ids drops nothing real. Making an empty reply
  with an unloadable reference stop borrowing the preceding message is correct;
  the "could not be loaded" notice branch handles it.
- **`tool_calls.py` prefix stripping:** correct. The `send_message` guard
  really never matched the prefixed dispatcher line.
- **`suppress_stale`:** same row filter and the same unvalidated status
  write as `update()` → `_change()`. It runs in one transaction and logs
  transitions after commit. Pruning `response_owners` matches its schema
  (`message_id` refers to `requests.message_id`).
- **Shell idle reaper:** the spin was real, because an uncontended
  `async with Lock` does not yield. The 1 s sleep is acceptable. Dropping
  stamps that have no tenant is safe, since stamps are only set by
  `_register_tenant`. I added the missing test.
- **Smaller fixes:** the GitHub `teardown` works (a retired context
  still resolves services through `_retired_services`).
  `knowledge_graph.drop_site` exists, and the RAG DB uses
  `check_same_thread=False`, so `to_thread` is safe. The `ListAdminServers`
  branch removal, the lockdown retry removal (the retry passed the name as
  `category_id` and could only error), the `manage_guild` gate for invites,
  the `_perms_to_caps` `is None` check, the `.env` `parents[1]` path, the BYOK
  script default, the `fetch_url` byte cap, the RAG input-error statuses,
  `conversation_memory.flush` with a shared future, the API body type checks,
  and the plugins/music/tts port parsing are all correct.
- **"Custom agents" removal:** I agree. Commit `bfdd89b` deliberately
  removed the agents feature and its `AgentProfileButton` wiring, and the
  leftover settings entry pointed at a dead screen.

## Disagreements and residual concerns (not changed)

- **`ListAdminServersTool`:** the first pass called the removed branch dead,
  but it was the **live** one. When `blocks` was empty, the first `if not blocks`
  returned "No cached Discord member/permissions…". The kept message was the
  one that never ran. The behaviour is fine either way, but the commit
  comment ("this is the only empty case") hides a change to the copy the model
  sees.
- **RAG 4xx handling:** treating 400/413/422 as input errors is right for
  oversized chunks. Some embedding servers return 400 for an endpoint-level
  problem, such as a model that does not support embeddings. Then the breaker
  never trips, and every message logs one warning and makes one wasted HTTP
  call. A consecutive-input-error threshold would close this.
- **Query embeds in `_embed_tasks`:** this is correct for `flush()`, but query
  embeds now count toward `MAX_BACKGROUND_EMBED_TASKS`. Under load, a burst of
  queries can make `_spawn` drop background embeds. The impact is small, but
  the tradeoff is undocumented.
- **Truncated tool calls:** prose that contains an unclosed
  `{"name": "..."` example now fails the stream with `ProviderProtocolError`
  where it used to post as text. This is acceptable because the router retries
  or falls back, but it is a behaviour change.
- **Heredoc scanner (`$((`):** an unclosed `$((`, which bash may parse as
  `$( (subshell) )`, leaves `arith_depth > 0`, and later heredocs are then
  ignored. This only affects the shell tool's shape hints, since commands run
  in the sandbox, so it is not a security issue.
- **Test isolation:** `tests/custom_harness.py` inserts `/root/maxwell`
  (the production checkout) into `sys.path`, so legacy tests that import it
  can test prod code instead of this worktree. This is pre-existing and outside
  `tests/unit`, but it weakens the claim that the full `tests/` failure set
  stayed byte-identical.
- **Untested first-pass fixes:** these still have no direct tests: the
  provider media text-only degrade, the RAG breaker statuses, the
  `user_install` empty-payload check, the GitHub teardown, the `drop_site`
  call, the recovery-floors tolerance, the purge `HTTPException` handling, and
  the TTS transcode fallback. I reviewed them by reading the code only.

## Not done

- I did not run the full `tests/` tree in the `maxwell:local` container, and I
  restarted no services. Nothing was pushed.
