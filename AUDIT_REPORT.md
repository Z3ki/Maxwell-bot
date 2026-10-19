# Maxwell codebase audit — findings and fixes

Branch: `fix/codebase-audit`. Method: manual review of the priority modules plus a
fan-out audit (19 module clusters, each candidate finding then attacked by two
independent verifiers — a correctness reader and a reproduction attempt) across
the whole tree. 73 candidate findings were raised; 19 were confirmed, 10
uncertain, 44 refuted (many of the refutations are "the working tree already
contains the fix", i.e. they were raised before the fix landed).

Every fix below is small and surgical, keeps the public command syntax, tool
names and user-facing copy (except where the copy was factually wrong), and is
covered by a regression test where a test could be written. Nothing under
`data/`, `data_gf/`, `logs/`, `.env*` was touched.

## Verification

| Command | Result |
| --- | --- |
| `python3 -m pytest tests/unit -q` | **679 passed** (645 before this branch) |
| `python3 -m ruff check .` | **All checks passed** |
| `python3 -m ruff format --check tests/unit` | **24 files already formatted** |
| `python3 -m mypy --python-version=3.12` | **Success: no issues found in 63 source files** |
| `python3 -m pytest tests -q --continue-on-collection-errors` | 50 failed — **byte-identical failure set to a pristine `HEAD` checkout** (see "Pre-existing failures") |

New regression tests: `tests/unit/test_tool_memory_marker_unit.py`,
`tests/unit/test_hydration_ordering_unit.py`,
`tests/unit/test_audit_regressions_unit.py` (27 tests). Every non-trivial fix was
checked to fail before the change and pass after (verified by restoring the
`HEAD` version of each touched module and re-running).

---

## Bugs fixed

### Critical / high impact

1. **`plugins/shell/impl.py:259` — idle reaper busy-loops and wedges the event
   loop.** `_idle_reaper_loop` computed `remaining` over *all* tenants, including
   ones currently running a command (their stamp is only refreshed in the
   command's `finally`). When every past-idle tenant was active, `expired` was
   empty and the loop re-ran with **no `await`** — an infinite CPU spin on the
   event loop. Now the loop yields when nothing was reapable, and drops stamps
   whose container is gone.

2. **`bot_parts/tool_calls.py:340` — `send_message` memory guard never matched.**
   The dispatcher passes `_remember_tool_call` the line `"Tool <name>: <result>"`,
   but the guard tested `result.startswith("__MESSAGE_SENT__")`, which can never
   be true for a prefixed line. Every delivered chunk was therefore *also* stored
   as a duplicate `is_tool=True` memory row on top of the real message row, which
   the model then replayed as its own echo. (Unit tests passed the raw marker, so
   the mismatch was invisible.) The prefix is now stripped before the marker
   check. Test: `test_tool_memory_marker_unit.py`.

3. **`context_hydration.py` — two hydration defects.**
   - `:336` an empty reply (e.g. a sticker reply) whose referenced message could
     not be loaded fell back to the *previous channel message* and presented it
     to the model as the reply target. The fallback now applies only to bare
     pointers (`^` / "this"), so the intended "could not be loaded" notice runs.
   - `:232` `preceding_non_bot_message` could resolve a pointer to a message
     *newer* than the pointer (the history adapter's no-`before` fallback, or
     stored memory rows). Ids are now compared as snowflakes so only earlier
     messages can be returned. Tests: `test_hydration_ordering_unit.py`.

4. **`plugins/web/impl.py:291` — unbounded response read in `fetch_url`.**
   `max_bytes=None` disabled both the `Content-Length` guard and the per-chunk
   cap, so a model-invoked fetch on a URL streaming a multi-GB body could exhaust
   RAM. Now capped at 25 MB (matching the media tools' `MAX_SIZE`).

5. **`providers.py:1191` — a media turn never degrades to text-only when the
   fallback is dead.** With a text-only primary and a downed fallback, the
   pre-request guard left `endpoint` pointing at a *known-dead* endpoint when no
   usable media route remained, re-requesting it until the attempt ceiling and
   failing the turn even though a text-only request to the primary would have
   succeeded. The guard now strips the attachments and retries text-only.
   (Confirmed by two verifiers with a reproduction.)

6. **`rag_memory.py` — four defects.**
   - `:1122` any non-200 from the embed endpoint tripped the *global* embed
     breaker, so one oversized/malformed chunk (400/413/422) paused embedding for
     every channel. Input-specific statuses now skip without opening the circuit.
   - `:1116` the embedding error response body was written to WARNING logs;
     embed endpoints commonly echo the rejected input, i.e. private channel text.
     The body is no longer read or logged (batch path too).
   - `:1105` the breaker was not re-checked after waiting for the embed slot.
   - `:2624` `add_negative` stored `content[:1500]` but embedded the *full*
     content, so `_embed_and_store`'s `content=?` guard never matched and any
     negative longer than 1500 chars was never embedded.
   - `:1236` the detached query-embed task was never tracked, so `flush()` could
     close the shared session under it. It is now registered like every other
     background embed.

7. **`plugins/sites/impl.py:845` — `delete_site` stopped dropping the site's
   knowledge-graph node/edges** (the pre-split implementation did). Nothing else
   prunes them, so deleted sites lingered in the prompt block forever.

### Correctness / crashes

8. **`bot_parts/core.py:673` — `is_connected()` always returned False on a
   sharded client.** `discord.ShardInfo` has no `ws` attribute; it exposes
   `is_closed()`. The gateway watchdog therefore always saw a disconnected bot.
9. **`bot.py:151` — a malformed `MAXWELL_GATEWAY_TIMEOUT` killed the gateway
   watchdog** (`float()` raised before the loop started), disabling
   gateway-restart recovery. Now parsed defensively.
10. **`bot.py:242` — shutdown aborted on the first tracked task that had failed.**
    Only `CancelledError` was suppressed, so a background task that finished with
    an exception re-raised and stopped the rest of cleanup.
11. **`bot_parts/messages.py:800` — a non-dict entry in `bot_commands.json`
    aborted the batch before results were persisted**, so already-executed
    dashboard commands ran again. Non-dict entries are now skipped.
12. **`bot_parts/recovery.py:50` — a corrupt `inbound_recovery_floors.json`
    crashed `setup_hook`** and every gateway recovery event. Unreadable/invalid
    floors are now logged and treated as empty.
13. **`api/api_server.py:243` — RAG read endpoints 500'd on a fresh DB**
    (`no such table: vectors`). `_rag_query`/`_rag_query_one` now return
    empty/None for a missing table, so the dashboard loads before the bot's first
    run.
14. **`api/api_server.py:2282` — `plugins_enable`/`disable`/`install` raised
    `AttributeError` (→ 500) on a JSON array body.** Bodies are normalised to a
    dict (install returns a 400).
15. **`plugins/discord_messages/impl.py:1223` — the purge confirmation died on a
    transient `HTTPException`** from `fetch_message`, leaving the interaction
    permanently pending. Now handled like the delete path.
16. **`privacy_data.py:325` — `cleanup_data` indexed `data["actions"]` without
    validating the JSON shape**, aborting the rest of the periodic cleanup on a
    hand-edited/restored file. Now mirrors the store's own tolerant reader.
17. **`inbox.py:52` — an empty `inbox.json` read as empty but raised on every
    write.** Missing *or* zero-length files are now a fresh store in both paths.
18. **`plugins/tts_voice/impl.py:416` — a missing `ffmpeg` aborted TTS delivery**
    instead of using the documented mp3 fallback.
19. **`plugins/music/service.py:41`, `plugins/tts_voice/clones.py:104`,
    `plugins/music/backend.py:115` — malformed URLs/ports raised bare
    `ValueError`** (`url.port` outside the guard; unvalidated `LAVALINK_PORT`)
    instead of the documented `MusicError` / `False`.
20. **`plugins/discord_moderation/impl.py:774` — the lockdown category retry could
    never succeed** and replaced the accurate "category not found" error with a
    misleading "invalid category_id".
21. **`plugins/discord_guild/impl.py:225` — unreachable second return in
    `list_admin_servers`**; the actionable guidance was dead code.
22. **`plugins/inbox/impl.py:50` — the inbox overflow line compared against the
    pre-slice length**, so items past the 20-row slice vanished silently.
23. **`web_page_text.py:51` — an unclosed/mismatched skipped element poisoned the
    skip stack** and discarded the rest of the page (browsers auto-close these;
    `HTMLParser` does not).
24. **`tooling/helpers.py:389` — an unquoted `<<` that is arithmetic (`$((1 << 2))`)
    was reported as a malformed heredoc**, rejecting valid multi-line commands.
    Arithmetic-expansion depth is now tracked.
25. **`tooling/helpers.py:106` — the early `.env` preload resolved to
    `tooling/.env`** after the module moved, so the documented preload never ran.
26. **`tooling/helpers.py:730` — an all-false `discord.Permissions` object was
    treated as "not cached"**, reporting permission-less members/bots as
    unreadable instead of "no elevated permissions".
27. **`user_install.py:947` — the zero-width-space fallback for an empty
    user-install payload was unreachable** (the dict was never empty because
    `allowed_mentions`/`ephemeral` are always set).
28. **`maxwell_core/providers/streaming.py:155` — a truncated bare-JSON custom
    tool call was posted as the assistant's message.** A stream cut mid tool call
    now raises `ProviderProtocolError` so the caller retries/falls back, while
    ordinary prose containing a stray `{` is unaffected.
29. **`maxwell_core/providers/usage.py:93` — reported cache tokens were dropped
    whenever the provider omitted `prompt_tokens`** (the subset guard ran against
    a prompt size of 0).
30. **`message_quota.py:25` — a premium grant could cap a user *below* the
    configured free default** (fixed 500 vs. an operator default up to 100000).
31. **`bot_parts/sending.py:31` — a whitespace run longer than the chunk limit
    produced an empty message chunk.**
32. **`chess_game.py:941` — the chess store ignored the bot's `DATA_DIR`** and
    read `MAXWELL_DATA_DIR`, which nothing in the tree sets.
33. **`watch_policy.py:320` — the prompt's stated reply-pressure bar was
    hardcoded**, not the operator-configured `conversation_watch_pressure`.
34. **`runtime_lifecycle.py:70` — `SHARD_IDS` were not checked against
    `SHARD_COUNT`**, so a plausible typo made discord.py raise at construction
    and abort startup.
35. **`bot_parts/media_context.py:75` — the cached-media provenance tag was
    dropped**, so a cached emoji/sticker grid was later re-attached and described
    as a user image.
36. **`plugins/sites/impl.py:212` — `create_site` accepted a blank body and
    reported success** for a site whose URL serves nothing.
37. **`plugins/maxwell_extras/command_suite.py:43` — the leftover personal
    "Custom agents" screen offered only a Reset button that always raised
    `ValueError`.** The dead entry was removed.

### Security / privacy

38. **`plugins/inbox/impl.py:53` — the prompt-injection taint gate was armed only
    for email rows**, although every rendered row carries text chosen by someone
    else (friend-request names, group-DM names). It is now armed whenever any row
    is rendered.
39. **`plugins/images/impl.py:303` — up to 500 chars of the HD-image provider
    response body were logged**, which can echo the private prompt. Status only
    now.
40. **`rag_memory.py` embed-body leak** (see #6) — private text in logs.
41. **`api/config.py:29` — an empty `MAXWELL_CORS_ORIGIN` defeated the fallback to
    `MAXWELL_PUBLIC_BASE_URL`** (the shipped `.env.example` has it empty), so
    every admin JSON response carried `Access-Control-Allow-Origin: ""`.
42. **`plugins/discord_guild/impl.py:991` — `manage_invites` was gated on
    `create_instant_invite`**, but listing/revoking invites requires
    `manage_guild`; callers holding the permission that matters were refused.
43. **`scripts/rotate_byok_key.py:22` — the BYOK rotation script defaulted to a
    database path the bot never uses** (`<DATA_DIR>/byok.sqlite3` instead of
    `<DATA_DIR>/plugins/maxwell_extras/byok.sqlite3`), so it silently rotated
    nothing.
44. **`plugins/github_projects` — the service's `aiohttp` session was never
    closed** and the plugin defined no teardown, leaking a connector on every
    reload. Added `close()` + `teardown`.

### Concurrency / resources

45. **`message_pipeline.py:948` — `Watermarks.save()` ran in a worker thread while
    the event loop mutated `_marks`** ("dictionary changed size during
    iteration", dropped saves). The snapshot is now taken under an `RLock`.
46. **`message_pipeline.py:672` — `response_owners` was never pruned** and grew
    without bound while `requests` stayed capped.
47. **`bot_parts/recovery.py:93` — `_abandon_offline_inbound` did up to 10000
    fsync-per-row journal writes synchronously inside `setup_hook`.** Added a
    batched `RequestJournal.suppress_stale()` (one transaction).
48. **`conversation_memory.py:495` — concurrent `flush()` callers returned before
    SQLite was actually closed.** All callers now share one close future.
49. **`typing_status.py:112` — typing cleanup suppressed `asyncio.CancelledError`**,
    swallowing task cancellation.
50. **`discord_expressions.py:231` — the expression-refresh loop died silently on
    any error outside its narrow caught tuple.** The loop body is now guarded.
51. **`bot_parts/media.py` — up to 25 MB video blobs were written/read
    synchronously inside async methods**, stalling the event loop. Offloaded to
    `asyncio.to_thread`.
52. **`bot_parts/media.py:682` — `_emoji_grid_shown` grew one entry per channel
    for the process lifetime.** Added to `_PER_CHANNEL_STATE_CAPS`.
53. **`tooling/helpers.py` heredoc/`$((` handling** (see #24) — also removes a
    false rejection of valid commands.

### Tests that could not catch what they claimed

54. **`tests/test_tool_progress.py:603` and `:734`** — both deferred-post tests ran
    `start_defer()` and `stop()` in *separate* `asyncio.run()` calls, so loop
    teardown cancelled the pending post task and `stop()` had nothing to cancel.
    They passed even if `stop()` stopped cancelling. Both now run in one loop.
55. **`tests/test_tool_progress.py:678`** — the "never posted" test called
    `stop()` first, so `transition_to_final` short-circuited on `_stopped` and the
    `not self._posted` branch it names was never reached. Now drives the branch.
56. **`tests/test_tool_progress.py:759`** — the "tick vs stop race" test injected
    the private `_stopped` flag instead of calling `stop()`; the assertion was
    guaranteed by `_flush`'s own guard. Now calls the real `stop()`.
57. **`tests/test_tool_registry_drift.py:23`** — `test_known_tools_has_no_ghosts`
    was byte-identical to the previous test and never checked the direction its
    name claims; folded into the surviving test.

---

## Findings deliberately not fixed

- **`.env.example:342` — stale `MAXWELL_NOTICE_VERSION`.** A genuine bug (the
  shipped value pins `processing-notice-1`, so the notice-version bump never
  re-displays the disclosure), but the task explicitly excludes `.env*` from
  modification. Fix: set it to `processing-notice-3`, or delete the line so the
  `privacy.py` default applies.
- **`plugins/github_projects/impl.py:361` — a timed-out workspace command keeps
  running in the persistent container** (only the local `docker exec` client is
  killed). The fix mirrors the shell plugin's process-group kill, which is a
  non-trivial change to a security-sensitive execution path and cannot be
  exercised by the tests available here.
- **`plugins/shell/impl.py:230` — `_destroy_container_unlocked` re-registers the
  tenant**, so a timed-out tenant keeps a cache slot until idle reaping. The
  re-registration is load-bearing for the recreate-on-next-use path; changing it
  touches sandbox lifecycle (capacity accounting, `_ensure_container_impl`
  re-registration) and risks regressions I cannot fully test in this sandbox.
- **`scripts/set_env.py:20` — `${...}` values are interpolated by python-dotenv.**
  Verified experimentally that python-dotenv expands `${VAR}` in *unquoted,
  double-quoted and single-quoted* values, so no in-file escaping can preserve a
  literal `${...}`; the proposed fix does not work. A correct fix is a
  project-wide policy decision (reject such values, or load with
  `interpolate=False`), which would change how `.env` is parsed everywhere.
- **`privacy_data.py:161` — `delete_live_data` propagates preference-store
  failures.** The abort is deliberate ("abort before deletion if suppression
  cannot persist"), and converting it to a success-shaped result would report a
  privacy action that did not persist. I added user-visible feedback in
  `plugins/maxwell_extras/privacy_ui.py` instead of changing the core contract.
- **`plugins/maxwell_extras/command_suite.py` "Custom agents" screen.** I removed
  the leftover scope entry so the dead panel is unreachable. Building a real
  agents UI (the `agent_profiles` data is fetched but never rendered) is a
  feature, not a bug fix.
- **`providers.py:1191` media degradation**: fixed, but the audit's suggested
  hardening note (also excluding `media_broken` from the generic failover
  alternatives at ~line 1542) was not applied — the degrade path now handles the
  case, and widening that filter risks changing unrelated failover behaviour.
- **`tests/test_tool_progress.py:759` real-race version**: the test now drives the
  real `stop()` rather than injecting the private flag, which removes the false
  confidence, but it still does not construct a genuinely concurrent race
  (a channel whose `edit()` blocks). Building that deterministically needs a new
  fake; left as-is.

## Pre-existing failures (not introduced)

`python3 -m mypy` as configured (`python_version = "3.11"`) fails before
checking any project file: the installed numpy stubs use the 3.12 `type`
statement. Running `mypy --python-version=3.12` is clean. This is an environment
mismatch, not a code defect.

The sandbox's `discord` package is a broken mix (discord.py-self 2.2.0a shadowing
discord.py 2.7.1) whose `discord.ui` submodule cannot even be imported, so every
test module that imports `bot_tools` fails at collection. `tests/unit` (the
stated gate) is unaffected.

The 50 failures in the full `tests/` run are identical before and after this
branch (verified against a pristine `git archive HEAD` checkout) — they are
environment issues plus two pre-existing `tests/test_dashboard.py` failures.
