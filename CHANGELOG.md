# Changelog

## Unreleased — 2026-10-08

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
