# Changelog

## 0.1.15 — 2026-10-17 (release candidate; operator review required)

- Detach Premium workers from chat turns: launch returns a job ID immediately, Maxwell can keep chatting in the same channel, and jobs own their provider/deadline, lifecycle and completion delivery. Add conversational status/cancel and private `/tasks`; preserve visibility after token expiry, avoid duplicate notifications, include jobs in deletion/drain/shutdown and never replay interrupted jobs. Persist metadata only; reports remain in bounded RAM.
- Make Premium substantial work coordinator-led: delegate sites, code, shell, research, repository work and scoped analysis to bounded workers, with independent `AI_WORKER_*` defaults and separate encrypted user worker BYOK/settings in Discord and the dashboard. Preserve live permissions, shared tool budgets, cancellation and honest deletion across both credential stores.
- Add configurable LLM/whole-task deadlines, context budgets and reasoning effort controls, plus owner-granted Premium background task workers, custom profiles and model-selected tool permissions within caller authorization.

- Remove hosted-provider presets and vendor-specific key links; use explicit endpoint/model/key connections, migrating encrypted historical records safely.
- Optional conversation storage now requires an affirmative choice. Historical defaults do not imply consent; resets preserve privacy opt-outs.
- Versioned optional consent and one-time private interaction disclosures; no legal DMs, public consent messages or blanket agreement gate.
- Privacy controls now review/withdraw choices, authorize an exact voice reference, request private operator support, and report deletion failures and manual/external limitations accurately. Discord and dashboard share settings.
- History, trace, usage and authentication retention cleanup; retired facts and legacy reasoning payloads are removed. Automatic error reports no longer copy the requesting message.
- Restrict server listings and voice presence to authorized context; close site metadata/KV ownership gaps; fix private backend reads, subpath KV guides and production GitHub workspace names. Fixes #74, #75, #76, #79, #80, #81.
- Integrate the modular bot split (#82), restore its missing core module, and preserve dev quota behavior. Remove the unused autonomous activity engine, routing and operator endpoints.
- Rewrite Terms, Privacy and Contact without invented jurisdiction/operator facts or blanket compliance claims. Publication is gated by `release-readiness.json` and reviewed deployment evidence. Premium sales remain subject to operator review.


## Unreleased

- Keep subagent output: files a job reports from `/workspace` are copied to the requester's `/storage/jobs/<id>/` and attached when it finishes, and the sandbox stays pinned while a job runs. Fixes the lost `.mcaddon` after the 10-minute idle wipe.
- Add Premium persistent storage (`user_storage` plugin): 100 MB per account at `/storage` in the shell, restored after every idle wipe, with a `storage` tool, signed private links and a public folder served at `/u/<handle>/`.
- Add Premium schedules (`schedules` plugin): cron or one-time wake-ups that run a saved prompt as a background job and post the result. Maxwell can create them through the `schedule` tool.
- Add `/dashboard` one-time sign-in links and new dashboard views: Overview, Files (upload, edit, rename, delete, links, handle), Schedules and a redesigned layout that works on phones.
- Premium shell commands get a longer per-command limit (`MAXWELL_SHELL_PREMIUM_TIMEOUT`).
- `get_configuration` returns resolved shell, storage, schedule, site backend and KV limits through a `limits_for` tool hook.
- Site backends that fail to boot now report the whole last traceback. The KV API guidance says plainly that anyone can write to it.
- Rebuild the public website and guide for current features; fix the privacy and terms pages that still described saved history as off by default.
- Fix `deep_test_harness.py` crashing on startup (#126) and stale documentation (#127).

- Preserve Maxwell mentions used as sentence subjects or command targets in model input as `@Maxwell (you)`; remove addressing edge pings without dropping other users' named mentions or misclassifying them as pointer-only turns.
- Notify the original message author on ordinary and detached worker replies while keeping everyone and role mentions disabled; interaction follow-ups remain mention-suppressed.
- Return short, tool-provided publication links from completed website workers instead of truncating model-written descriptions that can omit the URL.
- Do not post a "Maxwell is using" line for reactions, silence, typing, waits, or message edits, deletes, and pins. Those actions are the response, and the status line only appears and disappears.
- Remove generic working acknowledgements during streaming and app commands, and the persistent shell status that repeated once per command. Keep concrete tool progress and final answers.

- Offer chess and checkers move, state, and resign tools only while that channel has a live game, and attach them on the next step after start.
- Hide bot status, activity, avatar, and nickname tools from anyone who is not an operator. Only a site's owner can edit or overwrite it.
- Stop sending a sampling temperature. Remove the env knob, the personal-connection creativity field, and the provider retry that rewrote temperature.
- Tell each turn which chat model is answering, including a personal connection's model id, and return that id from get_configuration. Keys and endpoints stay hidden.
- Document how a server's disabled chess plugin hides its tools despite successful startup registration, and how to restore it without changing other server settings.

## 0.1.14 — 2026-10-14

Install with `--version v0.1.14`. Release assets include source, installers, checksums and the versioned container image digest.

- Match custom site API roots and arbitrarily nested routes before Caddy's static handler, including Dev routes; prevent working backend endpoints from returning public static-file 404s.
- Bake pinned python-chess into the trusted shared site runtime and advertise legal-rule support without permitting generated package installs. Existing site containers remain unchanged until explicitly restarted.
- Add owner/admin-only `site_server(action=request)` for bounded public API verification with fixed HTTP methods and JSON bodies, no redirects or path escapes, and private mutation restrictions; avoid blocked shell HTTP calls.
- Include the configured public-root prefix in site contracts and frontend guidance so Dev sites target `/dev/bot/.../api/` instead of Main's 404 endpoints.
- Preserve sticker-bearing final replies after tool progress by using the normal sticker delivery path instead of the text-only placeholder transition (GitHub #73); keep numeric sticker directives intact during transcript-mention cleanup.
- Fix elapsed playing/watching/listening/competing activities to serialize Discord timestamps in milliseconds instead of calling an unavailable SDK constructor.
- Resolve animated reactions and message emojis by live Discord ID, preserving renamed emojis and application assets with colliding server names. Accept reaction IDs/API notation, expose animation/sticker formats, support sticker ID markers and verify PNG/APNG/GIF/Lottie native dispatch. Clarify bot animation capabilities without Nitro and use compatible WebP emoji previews.
- Keep chess model context text-only: FEN, last move and legal moves with tactical notes. Show board PNGs only in Discord, render them once per update, and avoid image payloads, duplicate text boards and growing full move histories in tool results.
- Only explicit `[STICKER (name)]` markers become stickers. Ordinary brackets such as `[Help]`, `[OK]`, and `[100]` stay in the text.
- Privacy Off also deletes Maxwell's replies to that account, quoted text stored on those replies, and tool records from that account's turns. Another person's own message stays, without that quote.
- Send GIF replies as the GIF file alone. Reaction phrases use the keyless nekos.best catalog. Other phrases still use Giphy when `GIPHY_API_KEY` is set.
- Add a `/config` Privacy choice for message-content storage. Off stops saving that account's messages and deletes stored messages and facts for that Discord user. A live request is still answered and still sent to the AI provider.
- Add optional Giphy GIF replies, reaction-only completion and read-only-channel ping acknowledgements without forced text or retry loops.
- Refresh application emojis and official Discord sticker packs every five minutes; handle live server emoji/sticker updates, animated expressions, joins and removals while preserving server isolation.
- Keep a pointer such as "this" when the same message already has an attachment, embed, or sticker. Drop it only when there is no media and no earlier message to resolve.
- Keep qualified Last.fm plays when the provider returns error 8, an HTTP 5xx response, or an operator key error (10, 13, or 26). Retry those with the existing backoff. Remove a play only for a real per-track rejection.
- Lower the default free message allowance from 300 to 200 messages per five hours. An operator-granted premium plan uses 2,000 messages in that window and `AI_PREMIUM_MODEL` when it is set. Billing, prices, and checkout stay off. A fresh install still leaves the quota disabled.

- Remove automatic first-use Terms/Privacy DMs and their per-user records/locks from the runtime. Keep operator-specific policy links in shared model context even with tools disabled; offer them when asked, never invent another instance's policies. Preserve existing notice/consent data without reading or rewriting it.
- Add guarded isolated Dev lifecycle and offline ephemeral pytest commands, optional local-model/music services, and a reviewed dev-to-main promotion workflow without automatic public deployment.
- Stamp Dev images with their checkout revision and document draining the bind-mounted public bot before updating source, so restart/promotion diagnostics identify the applied revision without mixed live code.
- Wait for the lazy Lavalink connection's ready handshake on the first music request instead of returning a false node-outage error. Bound readiness waits, share startup across callers and wake pending waits on shutdown.
- Replace unattended YouTube device polling with explicit operator authorization that handles HTTP 400 pending/slow-down responses, expires cleanly and saves refresh credentials only to a private env. Keep OAuth enabled without polling on node startup.
- Recreate Dev's cipher in the current Lavalink network namespace when applying audio changes, rather than retaining a namespace belonging to the replaced node.
- Move conversation-history reads, writes and cleanup onto one FIFO SQLite worker. Add a covering cleanup index and an atomic stale-channel sweep; preserve scoped retention, archived rows and the 12-hour inactivity policy without stalling Discord.
- Run request receipts, transitions, retries and effect/delivery checkpoints on a FIFO SQLite worker with WAL and FULL durability. Await irreversible-effect checkpoints and retain cancellation ownership during journal waits; drain journal work on shutdown.
- Give the cipher sidecar 512 MiB with no swap instead of the pressure-prone 256 MiB budget; deployment is still explicit.
- Recall bounded older transcript passages with requester-scoped FTS5 search, keeping recent history cache-stable and older text reference-only. Maintain the index across upgrades, edits and deletion; preserve archived rows and shed optional recall first when budgets shrink.
- Add private opt-in Last.fm linking through `/lastfm` and the `lastfm_account` AI tool, with browser approval and explicit finish rather than a callback server. Encrypt owner-bound tokens/session keys and isolate Dev account data.
- Scrobble each linked, non-deaf human listener's actual unpaused voice listening after Last.fm's threshold, including repeats and eligible autoplay. Persist qualified plays off-loop, retry provider outages without holding music locks, and remove pending work on unlink/relink. Disconnected bot voice state cannot reactivate stale listeners; late-linked listeners use their own listening-start timestamp.
- Keep Last.fm browser approval tokens out of self-DM transcript/component context. Expire unused approvals in bounded off-loop batches and release idle owner locks, completed-upload counters and obsolete consent fences.
- Drain in-flight turns on SIGINT and SIGTERM before closing the gateway. New messages stop immediately. `DRAIN_TIMEOUT_SECONDS` defaults to 30. The process supervisor and Docker stop grace wait longer than that drain.
- Shard the Discord gateway automatically. `SHARD_COUNT` and `SHARD_IDS` select a manual cluster. Shard ready and resume resync that shard without starting a second set of workers.
- Hydrate `^`, `^^`, `this`, `that`, and Discord reply references into the live prompt. An unresolved pointer is not sent to the model alone.
- Run Discord tools, including music, from `/maxwell` in the server where the command was used. Private replies still go only to the requester and cannot be posted to another channel.
- Send `/maxwell` rich replies through the interaction webhook even when the source channel denies view, send, or embed permissions. Server tools still see those real channel permissions.
- Play YouTube again after the login wall. The September 2026 source plugin, a burner-account TV token in `.env`, and a local `yt-cipher` sidecar decode streams that plugin 1.18.2 refused. Title search falls back to YouTube Music when YouTube search returns nothing.
- Keep permission-checked music controls available after web research so identifying a track does not lock playback for the rest of the turn. Preserve music action serialization, Discord authorization, source restrictions and the destructive-tool taint gate.
- Update one persistent public music player across songs and queue changes, replace deleted/uneditable/moved panels, reject superseded controls, and keep transient Discord errors from spamming replacement embeds. Provide private queue/search/action feedback, public status fallback and ordered multi-song slash batches.
- Add opt-in session autoplay using real YouTube radio recommendations after the requested queue ends. Preserve queue/repeat precedence, avoid recent tracks, stop automatic continuation in empty channels or on source failure, and disable autoplay on Stop.
- Replace the audio image's obsolete Java 18 runtime with digest-pinned Temurin 21.0.12.1 LTS. Correct cgroup v2 CPU/memory detection on newer kernels and retain G1 GC with quota-sized workers instead of oversubscribing the 1.5-CPU audio container. Keep the Lavalink server and YouTube plugin versions unchanged; check JVM resource detection in CI.
- Prepare the next autoplay recommendation while the current song plays, with at most two background preparations across guilds. Keep unrelated source searches out of the playback-state lock so queued successors and Skip do not wait for them; preserve play-request order, recheck authorization/limits at commit, and invalidate stale additions on Stop, Clear or Leave. Preserve explicit queue/repeat precedence and document remaining YouTube stream startup and unmeasured audio capacity.
- Preserve full textual tool results through shell capture, repository inspection, plugin reads/diffs, web reads, backend logs, and follow-up rounds. Retire shell command/output size caps and pass large scripts over stdin. Provider context windows, Discord payload limits, and sandbox resource boundaries still apply.
- Share the site route contract across runtime and tool guidance. Resolve frontend endpoints against the same-origin site API mount so nested pages work; reject literal public mounts in Python route declarations before overwriting source.
- Keep app-action options out of stored user text, apply shared response/search defaults once, and clean legacy prompt wrappers without rewriting stored source material.
- Record every send_message chunk under its real Discord message ID; compact legacy tool/synthetic echoes while retaining intentional repeated sends, failures and cross-channel receipts.
- Deduplicate signed media URLs in generated context, emit one historical timestamp, reference retained reply parents by message ID, and use budgeted speaker mappings after the cacheable transcript.
- Play the video from YouTube watch/Mix links containing list=; keep explicit playlist-only links supported. Make /music status report current playback, pause state, channel, position, volume, repeat and queue length, including idle/offline status.
- Keep the audio bytes on app-command replies when the bot cannot post in that server. A refused channel upload is rewound before the interaction follow-up, and an empty TTS attachment is removed.
- Add a native AI DJ plugin with shared guild music queues, on-demand voice context, permission-checked playback, slash commands and Now Playing buttons.
- Stream YouTube videos, Music links and playlists through pinned DAVE-compatible Lavalink 4.2.2 / Lavalink.py 5.11.0 / YouTube Source 1.18.2. Provide private authenticated Docker audio deployment, reconnect recovery and idle cleanup.
- Accept direct public audio/video reference URLs in TTS without requiring message attachments. Support `reference_audio_url` and URLs in `reference`; preserve SSRF and redirect protections, remove local audio byte/duration caps and send the full reference for provider validation.
- Keep provider model-awareness routing compatible with providers that do not expose a model field.

## 0.1.13 — 2026-10-08

Install with `--version v0.1.13`. Release assets include source, installers, checksums and the versioned container image digest.

- Let Maxwell inspect, change and reset personal and authorized server settings through conversation, sharing storage with Discord /config and the dashboard. Keep global controls limited to configured application owners.
- Add requester-bound buttons for the existing private configuration and AI connection forms; keys never enter conversational tool arguments. Recheck user, server context and permissions on each action and menu click.
- Teach Maxwell to use live settings, tools, usage and running revision data when explaining its own capabilities; confirm changes only after saving.
- Replace the external provider usage URL with a caller-only `usage` tool that reads Maxwell's local message allowance, reports used/remaining percentages and rolling-window recovery, and recognizes unlimited accounts.
- Add a Discord-login web dashboard for personal reply settings, encrypted AI connections, and servers owned or managed by the signed-in user.
- Put language, answer length and visibility directly on the first `/config` screen, with an optional dashboard link.
- Share settings across Discord and the web, recheck server permissions on every dashboard read/save, and keep dashboard sessions separate from operator access.
- Lock scoped settings writes to preserve other users and servers, reload web ticket changes live, and document OAuth / dedicated-host Caddy setup.

## 0.1.12 — 2026-10-08

Install with `--version v0.1.12`. Release assets include source, installers,
checksums, and the versioned container image digest.

- Rebuild `/config` around a button-based home, direct settings screens,
  Back/Close navigation and separate personal, server and bot-owner tabs.
  Reply visibility and detail are editable together; language has presets
  and a custom form. Existing personal preferences survive the upgrade.
- Replace the five-field BYOK form with a guided provider connection using
  an API key and model name, plus a URL only for custom providers. Keep model
  capabilities, reasoning and response limits in Advanced. Show the active
  connection separately from setup selection, preserve saved keys/settings
  during edits, and confirm before removing a connection.
- Explain OpenAI API billing and app-request scope; show actionable, redacted
  connection-test failures. Acknowledge before storage work, cache redacted
  status for immediate form opening, and reject stale or closed forms.
- Make selected server tool groups mean allowed, use Discord's full channel
  picker instead of truncating it to 24 channels, and remove retired message
  allowance controls from the settings UI. Keep current permission checks.
- Add regression journeys for connections, preservation, confirmation,
  permissions, stale forms, provider failures, large servers and Discord
  component limits.

- Recreate the Maxwell container on installer updates even when the image and
  Compose configuration are unchanged, stopping tasks from older imported code.
  Document the separate Dev restart required after updating its checkout.
- Store tool-call, chess, and autonomous-message history under its originating
  guild so future server prompts include the recorded actions (#62).
- Read autonomy history with a channel-scoped requester, only after successfully
  accessing that channel's Discord history (#63).
- Retire finished chess games silently after the idle window, preserving
  cancellation notices for abandoned games and persisting lazy expiry (#61).
- Check existing version tags before the release build or publication, skip
  versions owned by another commit, and serialize release runs so workflow edits
  cannot overwrite a published version's container image (#64).

## 0.1.11 — 2026-10-06

Install with `--version v0.1.11`. This release groups all changes since
0.1.10. Assets include source, installers, checksums, and the container digest.

- Publish validated version bumps on `main` through the release workflow,
  retaining tag-triggered releases and publishing only the current changelog section.

- Remove global live-reply and inference admission caps, provider/shared HTTP
  connection caps, and the shared tool-dispatch budgets. Independent channels
  and servers start concurrently for primary and fallback models. Ignore old
  `ai_concurrency` and `MAX_PENDING_REPLY_REQUESTS` settings, remove per-channel
  admission caps and message quotas, retain conversation ordering and resource-specific
  containment, and track active inference calls.
- Replace live RAG with scoped SQLite conversation history. Remove vector,
  entity/graph and shared-context retrieval, embedding requests/recovery workers,
  and NumPy from runtime dependencies. Preserve the existing database and archived
  records for upgrades/export; old controls cannot restore retrieval.
- Refresh matching PR tracking refs after force pushes/rebases (#57).
- Deliver generated images through request-local attachment handles without
  public hosting or shell access; advertise public URLs only when configured
  and fail hosting tools clearly when no base URL is set (#58).
- Put isolated Git snapshots/clones on disk, share or reflink sanitized existing
  objects, reuse a private disk cache across separate mounts, avoid rewriting
  unchanged packs, and enforce the 4 GiB object limit
  with a clear error instead of exhausting tmpfs memory (#59).
- Keep guild emojis/stickers and reaction snapshots after the transcript,
  use the same history window for short and long requests, and trim oversized
  histories in message chunks. Sort authorized tool catalogs and schemas so
  registry reloads preserve their prefix. Preserve provider-reported cache
  reads/writes in request-local usage, timings and logs; show cache reads and
  the input hit ratio in `/debug`, distinguishing missing metrics from zero.
- Remove REM, context fact extraction, automatic memory summaries, delegated
  background workers, scheduled GitHub maintenance and automatic code repair.
  Remove their API routes, commands, settings and auxiliary model configuration;
  saved settings cannot reactivate them. Keep scoped recent history and the
  separately opt-in conversational autonomy feature.
- Consolidate Discord and tool instructions, keep personality limited to style,
  require authenticated permission context, and distinguish reply drafting from
  external actions. Defer terminal replies until action results have been read,
  return confirmation for state changes, and suppress duplicate replies in a batch.

## 0.1.10 — 2026-10-06

Install with `--version v0.1.10`. Release assets include source, installers,
checksums, and the versioned container image digest.

- Default self-hosted installs to unlimited messages without legal DMs,
  premium discovery, or the public hosted runtime's retired-tool restrictions.
  Existing operator settings and owner/Discord authorization remain respected.
- Hide website, email, HD image, embedding, and shell integrations until they
  are configured. Keep autonomy/REM opt-in and generated sites permanent by
  default. Support `--with-shell` through both installer handoffs.
- Consolidate registration across 16 bundled plugins and lazily import tool
  implementations. Validate plugin manifest versions, honor contained Python
  entrypoints, and check feature/package dependencies before setup.
- Preserve commands registered by other plugins, prevent disabled premium
  commands from being restored, and honor the configured plugin data directory.
- Enable Discord reply-author notifications by default while suppressing
  mentions in generated text. Keep explicit quiet replies and deleted-parent
  delivery fallbacks working.

- Rewind consumed Discord attachments before fallback channel delivery, preserving
  the complete upload when a reply's parent disappears or reply state is missing
  (#54).
- Use `commit_message` for GitHub commits so tool dispatch preserves commit text
  instead of colliding with the Discord message context (#55).
- Isolate credentialed Git in a private metadata snapshot, ignore workspace
  hooks/configuration/helpers, pin GitHub remotes, and pass authentication over
  stdin rather than Docker arguments or environment (#56). Use Git-compatible
  HTTP Basic authentication for checkout, fetch, and push.

## 0.1.9 — 2026-10-04

Install with `--version v0.1.9`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

- Serialize GitHub token logout with OAuth writes across processes. Require
  explicit repository opt-in for custom commands and Git operations that can run
  hooks or filters; built-in inspection uses an isolated read-only snapshot.
- Move prompt construction, output cleanup, provider protocols, and scoped memory
  policy into shared modules with explicit interfaces and compatible imports.
- Preserve async cleanup during repeated cancellation, keep admission bounded,
  and isolate unrelated requests during interruption and shutdown.
- Make plugin setup and reload transactional, retaining core registries and
  removing registrations and resources belonging to failed or retired plugins.
- Deduplicate memory by provenance and migrate stored hashes atomically while
  preserving row IDs, metadata, and embeddings.
- Expand regression coverage for malformed provider streams, lifecycle failures,
  scoped memory, prompt construction, and visible output. Add `make check` and
  an 80% shared-core line/branch coverage gate to CI.

## 0.1.8 — 2026-10-04

Install with `--version v0.1.8`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

- The model still decides when to search. Prompts now cover changing facts, the current date, reading the source, and treating older web memories as historical.
- `web_search` tries SearXNG when `SEARXNG_URL` is set, then Tavily when `TAVILY_API_KEY` is set, then the keyless ddgs fallback. ddgs ships with the core install. A failed or empty provider falls back inside the same deadline, and useful partial results return without filling every slot.
- Identical in-flight queries share one retrieval. Workers, queues, and caches are bounded, and shutdown does not abandon a search that is already running.
- The model can request `freshness` of live, recent (two minutes), or stable (thirty minutes), and a publication `time_range` of day, week, month, or year.
- `fetch_url` keeps the requested HTML section, bounds redirect and fallback time, and strips credentials on cross-origin redirects.
- Optional SearXNG is `docker compose -f docker-compose.search.yml up -d` after `SEARXNG_SECRET` and `SEARXNG_URL` are set. See [web search configuration](docs/WEB_SEARCH.md).
- A link unfurl that replaces the live message no longer drops `reply()`. `send_media`, `send_file`, `send_meme`, and the final text reply post through the channel when reply is missing.

## 0.1.7 — 2026-10-03

Install with `--version v0.1.7`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

- Ask Maxwell responds directly to the selected message without a popup form, using saved reply preferences. Rewrite / Translate retains its request form.
- `image_generator` and `hd_image` return generated media to the model for inspection without posting to Discord. The model decides whether to send it, use it in a site, regenerate, or reply normally; image MIME types and full-resolution reusable files are preserved.
- A same-user interrupt releases its slot in the reply queue, so one busy channel cannot fill the process-wide cap.
- Purge confirmation deletes the selected messages on discord.py 2.7.
- Moderation requires the asker to outrank the target. Role edits and channel overwrites cannot grant permissions the asker lacks. Renaming or archiving a thread requires Manage Threads unless the asker owns that thread.
- AFK timeout must be a Discord-legal value. AFK, system, and voice-move channels must belong to the same server.
- A reminder whose channel is gone, or that keeps failing to send, is retired.
- Automatic mailbox context omits the sender, subject, and snippet until a tool reads the message and marks the turn untrusted.
- `usage` is operator-only and returns quota lines, not the raw upstream body.
- Plugin archives unpack into a fresh directory, so a zip named `...zip` cannot delete `data/`.
- Cookies from a proxied site stay on that site's path and cannot set a shared Domain.
- Public address checks reject multicast, deprecated site-local, and NAT64 addresses.

## 0.1.6 — 2026-10-03

Install with `--version v0.1.6`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

- Recover stale Discord attachment URLs through the source message or bounded channel history; preserve signed URL parameters and resolve accessible Discord message links to their media.
- Preserve JPEG and other image MIME types from media tools through provider follow-ups, and apply checked redirects and size limits to attachment fallback downloads.
- Merge live history chronologically, honor current message edits, preserve all selected history slots, and bound history reads while retaining partial results.
- Retry personal app requests with their live interaction adapter, recheck ignored users before queued execution, and keep interaction IDs out of gateway replay cursors.
- Execute side effects and visible replies in the model's declared order; overlap only adjacent tools explicitly marked read-only.
- Reach all configured provider routes and explain when media could not be delivered to a text-only fallback.
- Restrict the operator mailbox to operators, include automatic mailbox context only in private operator conversations, and track announced notices separately for concurrent requests.

See [the pipeline audit](docs/PIPELINE_AUDIT_2026-10-03.md) for root causes, regression coverage, and verification limits.

## 0.1.5 — 2026-10-03

Install with `--version v0.1.5`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

### Discord and revision history

- `/maxwell` uses normal text and brief replies by default; saved and explicit detail preferences still apply.
- Ask Maxwell opens a prompt form with the same actions, detail, visibility, and language as slash requests. Rewrite / Translate adds another entry point; quick message actions share the request defaults.
- DM app requests include available participants, richer message payloads, reply chains, and permitted history, including private DM requests. Missing history is stated explicitly.
- Maxwell can read its startup revision and changelog, recent GitHub commits, and changed-file summaries without assuming upstream changes are deployed.
- Reply visibility, responsive settings controls, selected-message context, and channel history up to 1,000 messages are supported.

### Generated sites

- Re-enabled `site_server` in the sites plugin and public dispatcher for real custom APIs, authentication, WebSockets, and server-side secrets. The `backend=true` flag remains the simple built-in KV API, not a custom server.
- New apps use the shared Python 3.12 + FastAPI/Uvicorn stack with one worker, no reload, and SQLite under `/data`. Existing Flask applications, source, databases, and secrets are preserved.
- Backend source, logs, environment, and lifecycle are owner/admin-only; private requests cannot mutate a public backend.
- Required host setup: `sudo bash scripts/setup_site_host.sh`, plus the Compose policy mount. The site pool is capped at 2 CPUs / 4 GiB total, each backend at 256 MiB / 0.5 CPU, and 64 backends maximum. Existing containers must be restarted into the protected pool without deleting their persisted data.
- Custom backend code runs as an unprivileged user under gVisor, with read-only source, private persistent data, no capabilities, no swap, bounded temporary storage, and rotated logs. Model calls reuse the trusted runtime image rather than building per-site images.
- The public API proxy limits active work to 32 connections per site and 128 globally, including WebSockets and SSE. Busy requests receive HTTP 503 instead of queuing without bounds.

## 0.1.4 — 2026-10-01

Install with `--version v0.1.4`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

### Search

- Maxwell does not run `web_search` before answering, and it does not append a Search references list. A lookup still happens when you ask for one, or through `/maxwell web=search`. `web=off` still disables web tools.

## 0.1.3 — 2026-10-01

Install with `--version v0.1.3`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

### Shell

- Shell guests resolve names through `1.1.1.1` and `8.8.8.8`. Docker's `127.0.0.11` stub does not answer on the gVisor sandbox network.

## 0.1.2 — 2026-10-01

Install with `--version v0.1.2`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

### Discord

- `send_media` names uploads from the URL path, the response type, and the file bytes. A QR link whose query contains `https://z3ki.dev` is uploaded as `.png`, so Discord renders it.
- An in-flight turn keeps running when the same person sends another message that does not mention, ping, or reply to Maxwell.

### Shell

- `scripts/setup_shell_host.sh` installs gVisor `runsc`, the cgroup, and the egress firewall in one step. `install.sh --with-shell` runs it. The default install still leaves shell disabled.
- The shell bridge is `br-maxwell-sh`. A longer name is rejected by Linux before the network exists.
- When Docker's storage driver cannot enforce a per-container size limit, the script moves only `overlay2` onto XFS with project quotas. Existing containers stay on the default runtime.
- The sandbox image is built from `docker/Dockerfile`.

### Providers and memory

- Gemini tool-call signatures are kept on the follow-up request, so a tool turn stays on the primary model instead of falling over to the backup.
- Recalled embeddings are writable. Search no longer fails with `output array is read-only` and drops long-term memory.
- Reminder delivery backs off for 15 minutes when a channel is gone, instead of fetching it every few seconds.

External Discord, provider, mail, and browser integrations remain dependent on the operator's configuration. Passing local checks is not a claim that every external integration is production-validated.

## 0.1.1 — 2026-10-01

Install with `--version v0.1.1`. `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

### Providers

- Chat calls go through independent OpenAI-compatible clients and a provider router. `AI_API_URL`, `AI_MODEL`, and `AI_API_KEY` stay the primary settings. Existing `OLLAMA_*` names and the `OllamaProvider` import still work.
- Each result carries its own content, tools, usage, and timing. Tool dispatch no longer reads shared provider leftovers from the previous call.
- Bring-your-own-key requests require verified HTTPS, public addresses, no redirects, isolated credentials, and a total deadline.
- `send_message` no longer posts its own progress line. The reply is the update.

### Discord

- Application-owner diagnostics in `/config` send the redacted embed and JSON export. Those clicks no longer fail with `TypeError`.
- The message-allowance form uses a label Discord accepts.
- Web-search answers keep numbered citations, including clickable links in plaintext and `send_message` replies. Citations stay with the request that produced them.
- Memory no longer stores raw media payloads. Attachments still reach the model on the tool follow-up.

External Discord, provider, mail, and browser integrations remain dependent on the operator's configuration. Passing local checks is not a claim that every external integration is production-validated.

## 0.1.0 — 2026-09-30

Initial versioned release. Install with `--version v0.1.0`; `main` remains a development snapshot. Release assets include source, installers, checksums, and the versioned container image digest.

### Installation and deployment

- Both installers support exact release tags (`--version`) and full commit IDs (`--ref`), resolve the revision once, and hand off to the installer from that same commit.
- Failed or incomplete downloads do not replace an existing installation. Updates refuse tracked local changes and retain configuration, memory, and generated sites.
- Runtime images and detached release checkouts can start without a moving `main` branch. Explicit container commands support diagnostics without starting another bot.
- Matching semantic-version tags run checks, build and smoke the Docker image, and publish source, installers, checksums, and a versioned GHCR image.
- Primary `AI_API_URL`, `AI_MODEL`, and `AI_API_KEY` settings work directly; an explicitly blank friendly key clears a legacy key.
- Local credentials and coding-agent configuration are excluded from Docker build context.

### Reliability and prompt efficiency

- Fragmented provider JSON is accumulated within its size limit. Invalid/error/truncated SSE responses are not accepted as completed answers.
- Learned output limits and required temperatures are isolated by endpoint and effective model; small output caps can be repaired without limiting fallback models.
- Nonfinite usage and cost fields do not corrupt accounting.
- The authenticated status API recognizes live bot children of its Docker supervisor instead of incorrectly reporting Docker deployments offline.
- Prompt budgeting preserves system instructions, live input, and native tool-call records. Complete historical transcript blocks are the expendable tier.
- Conditional tool guidance replaces duplicated always-on instructions, follows the requesting user/server's plugin access, and places reusable instructions before volatile requester context.
- Background-worker briefs follow stable instructions and use a distinct final-text delivery contract.
- REM limits the complete serialized short-term slice to 120,000 characters without dropping event identities; metadata-only overflow fails without consuming the slice.
- Explicit zero memory-tier caps are honored instead of allocating an uncapped transcript.

### Voice removal

- Removed voice-channel joining/listening, DM call answering, speech generation, tools, prompts, admin controls, and dedicated dependencies.
- Audio attachments remain supported as model input. Voice moderation of other members is unchanged.

### Discord request preferences

- `/maxwell` replies are public by default; saved private visibility and channel-context limits are honored unless explicit command options override them.
- `/config` opens privately with an overview, current selections, explanations, immediate-save feedback, reset controls, and Close.
- Preference reads reuse unchanged files while detecting external updates. Unreadable preferences fail closed to private replies without channel context; writes refuse to overwrite corrupt files.

### Public site

- The public site is a product landing page, separate from operator APIs. Hosted premium plans are not available in this release.
- Guide, contact, terms, and privacy pages share responsive styles and the Maxwell icon; the Caddy example explicitly allows these public routes and assets without exposing operator controls.
- Removed the browser admin dashboard, its static proxy routes, and Discord OAuth login/session flow. Discord `/config` and the authenticated Basic operator API remain available.

External Discord, provider, mail, and browser integrations remain dependent on the operator's configuration. Passing local checks is not a claim that every external integration is production-validated.
