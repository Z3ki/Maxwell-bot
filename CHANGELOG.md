# Changelog

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
