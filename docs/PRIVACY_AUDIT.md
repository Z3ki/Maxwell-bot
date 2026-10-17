# Maxwell 0.1.15 privacy, consent and release audit

Audit date: 2026-10-17. Baselines: main `33dc417`, dev `93790e3`;
modularization PR #82 head `8f42b79`. This is engineering evidence, not a legal
opinion or regulatory certification. Production credentials, provider accounts,
server disks/backups and the Discord Developer Portal were not available here.

## Discord requirements and design decisions

Official sources consulted:

- [Developer Terms, especially sections 3(b), 4 and 5](https://support-dev.discord.com/hc/en-us/articles/8562894815383-Discord-Developer-Terms-of-Service)
- [Developer Policy, permission, privacy and API data rules](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy)
- [App Directory inclusion guidelines](https://support-dev.discord.com/hc/en-us/articles/8852009977879-App-Directory-Inclusion-Guidelines)
- [Interaction responses and ephemeral flags](https://docs.discord.com/developers/interactions/receiving-and-responding)
- [Application installation contexts](https://docs.discord.com/developers/resources/application)
- [June 2026 privileged data access changes](https://discord.com/blog/updated-requirements-to-how-apps-access-data-in-servers)

| Requirement or choice | Basis and implementation |
| --- | --- |
| Accurate accessible privacy policy, current Portal link and deletion route | Required by Developer Terms 5(a)-(b). Repository changes do not update Portal settings. |
| Limit API data to the stated functionality, respect opt-outs, obtain permission for actions | Developer Policy and Terms. A server install is not individual consent for unrelated processing. |
| Third-party sharing | Terms 5(b) permits specified sharing including service providers; whether each configured processor qualifies and whether legal consent is needed requires operator review. No blanket assumption that every provider transfer is covered. |
| Encryption at rest and safeguards | Terms 5(c). BYOK/session encryption is implemented; the deployment must encrypt remaining SQLite/JSON data, disks and backups. |
| Universal Agree button for all conversations | No blanket requirement identified in the cited current Discord documents. Contract formation and local legal bases still need review. A delivered notice never records acceptance. |
| Saved-history opt-in | Privacy design choice here, not a claim Discord universally mandates opt-in history. Optional on/off evidence is separate from ToS agreements. |
| First-use information | A nonblocking private follow-up for application interactions, once per disclosure version. No DM, public prompt or additional AI call. Ordinary message events receive no fake ephemeral response. Policies remain available via /help and /config. |
| Voice reference permission | Feature-specific affirmative authorization for an exact reference. A public URL is not proof. Preset speech stays available. |
| Renewed agreement | The store can track independent agreement versions, but ordinary requests have no agreement gate. Operators must decide whether a material change requires renewed agreement; no per-deployment version bump. |

Discovery eligibility also depends on live Portal requirements, app review,
privileged intents and account settings. They cannot be verified from source.
For applicable law, review jurisdiction, scope and individual rights rather than
claiming blanket GDPR/CCPA compliance. See the [EU GDPR text](https://eur-lex.europa.eu/eli/reg/2016/679/oj)
and [California Attorney General guidance](https://oag.ca.gov/privacy/ccpa).

## Verified baseline findings

1. No first-use policy disclosure was sent; legacy notice state was unused.
2. The old agreement system was absent. No legacy record is imported as agreement.
3. `user_preferences._DEFAULTS.tracking` was `on`; missing stores also permitted storage.
4. `/config → Privacy` offered transcript/fact deletion. The dashboard API handled
   tracking but its browser form had no privacy selector.
5. Reset removed the tracking override and explicitly told users storage was on again.
6. Policy links were mostly model context derived from the deployment base URL.
7. Terms contained an unfinished bracketed governing-law clause.
8. Contact referred users to an unidentified operator and had no concrete private flow.
9. Some stores had count/idle bounds, but no common absolute age policy. Raw model
   reasoning and request text could enter diagnostics/owner reports.
10. Storage opt-out did not prevent provider transmission, and could be confused
    with a processing opt-out.

## Resulting user experience and implementation

New accounts and historical unverified defaults start with optional history off.
Ordinary mentions and DMs continue normally. App commands receive a short private
provider-processing disclosure once, after acknowledgment; concurrent interactions
claim one receipt. Failed delivery has no public/DM fallback and can retry later.
The receipt is not an agreement record. No AI request is used for privacy UX.

`/config → Privacy` and the dashboard explain optional history, current provider
processing and retired persistent facts/embeddings. On is an intentional choice;
model-driven configuration cannot enable it for a user. Off stops new transcript
writes and removes stored conversation content. Reset stays off. Independent
choices can be reviewed/withdrawn; voice authorization is bound to a hash of the
exact reference and a distinct policy version. It is a user's permission
attestation, not identity verification or proof of a third party's authorization.

Consent storage uses minimal SQLite records (user ID, policy, version, kind,
status, recorded and withdrawal times). JSON preferences use atomic replacement,
a process lock and cross-process file locks. Tracking checks fail closed, and the
history writer checks again after acquiring its SQLite write transaction. A
suppression timestamp prevents old in-flight writes from reappearing after an
intentional re-enable. Privacy state is independent of conversation history.

The no-history path skips stored-history recall and automatic app-history fetch;
the current request and specifically selected context remain available to answer.
Human rows, bot replies, edits and tool transcript writes carry their owner and
are gated. Unattributed bot/tool rows are not retained. Quotes of opted-out users
are stripped from metadata. Model-generated reasoning/parameters are no longer
persisted in the trace store. Intentional output publication, account linking,
reminders and workspaces have their own feature records and are disclosed as
separate from conversation history.

Private support is user-initiated from the Privacy panel and delivered to the
configured owner; delivery failures are reported. Its own delivered Discord
support copy requires operator handling. The support recipient must be tested
before release; no fabricated address was added.

## Retention and deletion inventory

| Category / location | Default lifecycle and deletion |
| --- | --- |
| `maxwell_rag.db` conversation/linked tool transcript | Optional; 30-day absolute age, existing per-channel count bound, 12-hour inactive-channel cleanup. Off removes owner rows/replies/quotes; full deletion also invokes it. |
| Legacy facts/vectors/user directory | No current live embeddings/fact creation. Cleanup removes retired facts/vectors and the old user directory. Archived modules remain offline compatibility fixtures, not live providers. |
| Personal settings JSON | Until changed/deleted. Full deletion replaces the row with storage-off plus a suppression timestamp. This minimal privacy record intentionally remains. |
| `privacy.sqlite3` | Active optional choices until withdrawal/deletion. Withdrawn choices: 365 days by default. Notice ID/version/time remains to avoid repeated unchanged notices, unless the user deletes it. |
| `llm_traces.json` | Metadata only; seven days / 300 rows. Legacy raw reasoning payloads are scrubbed. Full deletion removes matching user rows. |
| `inbound_requests.sqlite3`, quota DB | Seven days by default; no message bodies. Short-lived usage/security metadata may remain after personal-data deletion to prevent quota abuse. Existing quota/window pruning also applies. Manual plan/limit grants last until revoked or full deletion. |
| Dashboard flows/sessions | Five-minute flows, at most eight-hour sessions; prune during auth and periodic cleanup. User sessions are revoked on full deletion when the session key/store is available; failures are reported. |
| `task_agents.sqlite3` custom profiles | Explicitly saved instructions/scopes/tool permissions; ten per user, 90 days since last edit by default, periodic pruning and full deletion with stale-write suppression. Worker transcripts remain in memory only. |
| Encrypted BYOK credentials | Until explicit removal/full deletion; encrypted using the operator's configured master key. No key value is returned to a user/model. |
| Reminders / rich callback registry | Explicit feature records. Delivered/cancelled reminders are removed; callbacks have a TTL and periodic pruning. Full deletion filters supported owner records under the same file lock as writers. |
| GitHub token/policy/knowledge JSON | Removed for the user by full deletion. Running GitHub containers and filesystem workspaces need operator quiescence/removal. Environment-provisioned credentials need operator removal. |
| Last.fm accounts/outbox | Existing explicit browser authorization and disconnect lifecycle. Live full deletion attempts disconnect; failures are reported. Dashboard/offline plugin copies require operator review. |
| Generated attachments, TTS samples | Turn-scoped memory/temporary directories are cleared by tool lifecycle. Discord/provider copies are external. Explicitly hosted/published outputs need operator deletion. |
| Sites/KV/backends, shell/GitHub workspaces | Separate user-requested artifacts; do not delete a mounted/running filesystem blindly. Lifecycle-aware operator deletion is disclosed in every full-deletion result. Existing site TTL applies; permanent sites need explicit review. |
| Inbox, threads, chess, other plugin caches/queued jobs | Scope, ownership and lifecycle vary; require operator/plugin-specific review when relevant. Full deletion does not claim these were erased. |
| Host/proxy/container logs and backups | Outside repository-managed storage. Operator must set expiry/rotation, encryption and restore suppression; currently release-blocking. |

Cleanup runs on startup and roughly every ten minutes, outside the event loop.
History migration scans owners once and removes blocked content in bulk rather
than rescanning the entire transcript separately for every user. Errors are
reported by subsystem/type without deleted message content. Healthy stores are
not rewritten with fabricated defaults to hide corruption.

Deletion persists suppression first, cancels live owned requests where possible,
clears relevant in-process content caches, then attempts each supported store.
The dashboard writes the same suppression record read by the bot. Results list
failed stores, manual work and external/backup limitations, and never say all data
was deleted. Retries are safe. SQLite secure-delete reduces residual active-page
copies; this is not a forensic erasure guarantee for WAL, disks or backups.

## Migration and deployment

Historical `tracking=on` without current affirmative evidence is interpreted as
off, including values copied from old defaults. Explicit historical opt-outs stay
off. Old on values cannot reliably distinguish a clicked choice from a dashboard
snapshot, so users intentionally enable current optional history once rather than
having agreement invented for them. The old consent system is not trusted.

Startup cleanup intentionally removes unverified old content and retired facts.
Protect any operational backup, set its expiry, and retain the latest suppression
records outside historical snapshots. When restoring, quiesce writers, restore
only necessary data, replay the latest deletion/suppression records, run cleanup,
verify no deleted content is visible, then resume. This requires a real restore
exercise on the operator's infrastructure; it has not been claimed as tested here.

The Terms/Privacy/Contact pages preserve the dark site shell but are explicitly
marked for operator review. The unfinished jurisdiction clause and implied blanket
privacy consent were removed. No identity, company, postal address, jurisdiction,
no-training promise, geographic guarantee or compliance certification was invented.

Configure `MAXWELL_TERMS_URL`, `MAXWELL_PRIVACY_URL`, `MAXWELL_CONTACT_URL` or an
HTTPS `MAXWELL_PUBLIC_BASE_URL`. Explicit URLs take precedence; untrusted schemes
and credential-bearing URLs are rejected. Self-hosts never inherit a hosted
operator's policies. Configure Portal Terms/Privacy links manually and verify
anonymous availability and the application profile links. Check Discovery Status
and privileged-intent requirements in the Portal separately.

## Provider review

The architecture supports configured primary/fallback/vision HTTP providers,
generic endpoint BYOK, search providers, NVIDIA/image generation and
Mistral TTS. No live embedding provider is used. Source alone cannot identify
which account tier, route, endpoint or retention settings are active in production.
Provider secrets/endpoints are not disclosed by the privacy UI.

- Verify the configured API endpoint’s own published retention, subprocessors and routing rules; do not infer those guarantees from a model name.
- [Mistral voice usage](https://docs.mistral.ai/studio/audio/text_to_speech/voices): explicit speaker consent matters; public media is insufficient.
- [Mistral ZDR](https://docs.mistral.ai/admin/monitor-comply/zero-data-retention): availability/configuration must be checked, not presumed.
- [Gemini API terms](https://ai.google.dev/gemini-api/terms): service tier and age/use eligibility require review before routing general Discord users there.

The operator must complete an actual processor inventory, age eligibility,
contracts/terms, training/retention settings and transfer assessment for every
active route and optional integration, including fallback and user-selected BYOK.

## GitHub backlog and modularization

- #74: permit private read-only site/backend actions at the central dispatch gate.
- #75: include the configured public-root path in built-in KV instructions.
- #76: preserve production's legacy GitHub container name; dev remains isolated.
- #79: apply ownership checks to metadata, KV, rename and lifetime changes.
- #80: restrict guild listings to the current server, filter invisible channels
  and mask invisible member voice channels; listing all bot servers is owner-only.
- #81: lookup_user reports voice state only in the invoking server and a channel
  visible to the requester; DMs cannot disclose cross-server voice presence.
- #82: integrate the mechanical split, restore `bot_parts/core.py` (omitted by an
  overly broad `core.*` ignore), preserve dev quota changes and run the full suite.

The unused autonomous engine, social planner, routing shim, commands and HTTP
endpoints were removed. The useful reply-label formatter was moved into prompt
history rather than keeping an engine dependency. Tests for retired capabilities
were removed; tests for still-supported history writers now explicitly opt in.

## Release status and outstanding operator actions

This is the next candidate, **0.1.15**, not an assertion of public-release readiness.
`release-readiness.json` and `scripts/check_release_readiness.py` prevent the release
workflow from publishing while operator evidence is absent or policies are still
marked pending. Source fixes may be merged into dev/main without publishing a
paid/public release. Complete and review the evidence, update the public policies
to match the actual deployment, then rerun CI and the release gate.

Outstanding actions: operator identity and contract/legal decisions; actual private
support recipient/delivery; Portal link/discovery verification; processor inventory
and age/transfer assessment; encryption for all stored data; host/proxy log and
backup expiry; manual plugin/workspace deletion and restore drill; real billing,
renewal, cancellation, tax and refund terms before Premium sales.

Expanded provider/worker verification results and final Git references are recorded in the PR and
final delivery report. No deployment/restart or Portal modification is implied by
merging source.

## Initial privacy-only verification

- Python 3.12: 3,149 tests passed, two explicitly skipped (no local Chromium and
  no real provider API key), nine existing Discord UI deprecation warnings.
- Shared-core branch coverage: 83.01%, above the configured 80% gate.
- Ruff, compileall, pip dependency check, 11 tracked shell syntax checks and
  dashboard JavaScript syntax check passed.
- Scoped-history and inbound-queue benchmarks ran at 100, 500 and 1,000 servers.
  These are local storage/admission measurements, not Discord/provider capacity.
- Docker is unavailable locally; pinned audio/runtime image checks are delegated
  to the repository's GitHub Actions workflows, not claimed as local passes.
- The release-readiness check intentionally blocks publication pending operator
  evidence. No live Discord/Portal/provider or backup-restore validation is claimed.

## Provider preset retirement

Hosted-service presets and key-purchase links have been removed. Discord,
dashboard and installers use explicit URL/model/key connections. Only the generic
protocol kind is offered for new BYOK saves. Earlier encrypted preset rows are
migrated with a compare-and-swap to the generic identifier, preserving their exact
original endpoint and key; deletion or a concurrent edit wins the migration.
Changing an endpoint requires entering its API key again. The read-only legacy
upgrade map cannot create new presets or route fallback traffic.

## Provider controls and Premium workers

The follow-up engineering scope adds request and whole-task timeouts, context size
and reasoning effort controls, shared between Discord and dashboard. See
[PROVIDERS.md](PROVIDERS.md) and [BYOK.md](BYOK.md) for bounds. Manual Premium grants
are available only to configured owners in `/config → Bot controls`. Paid checkout
remains disabled. Full deletion revokes manual plan/limit grants; short-lived quota
metadata remains under the documented retention policy.

[PREMIUM_TASKS.md](PREMIUM_TASKS.md) documents request-scoped workers: same caller,
provider and permission checks, no saved transcripts/background scheduler, bounded
steps/time/concurrency, cancellation joined with the parent, and explicit partial
failure reports. New tests cover grants/revocation/restarts, failed plan writes,
worker cancellation, privacy suppression, rejected tools/exports/recursion,
capacity and context budgets. These workers do not restore the deleted autonomous
engine or its legacy controls.


### Independent worker providers and coordinator routing

Premium substantial tasks now use request-scoped workers with separate operator
`AI_WORKER_*` settings or an independent personal worker BYOK slot. Dedicated
worker BYOK wins over chat BYOK, then operator workers, then the primary provider.
Task text, selected tool results and supported transient media go to that route;
chat remains on its own connection. No fallback bypasses a saved unavailable key.
Worker BYOK records live in `plugins/maxwell_extras/worker_byok.sqlite3`, encrypted
under the same master key as chat BYOK, retained until removal/full deletion.
Full deletion reports worker-vault failures separately and invalidates stale
credential saves. Rotate both databases before switching the master key.
Worker transcripts remain transient; no separate worker history or consent gate.
Broader reviewed task tools retain central caller, plugin and scope checks.
