# Changelog

## Unreleased

- `/maxwell` uses normal text and brief replies by default; saved and explicit detail preferences still apply.
- Ask Maxwell opens a prompt form with the same actions, detail, visibility, and language as slash requests. Rewrite / Translate adds another entry point; quick message actions share the request defaults.
- DM app requests include available participants, richer message payloads, reply chains, and permitted history, including private DM requests. Missing history is stated explicitly.
- Maxwell can read its startup revision and changelog, recent GitHub commits, and changed-file summaries without assuming upstream changes are deployed.
- Reply visibility, responsive settings controls, selected-message context, and channel history up to 1,000 messages are supported.

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
