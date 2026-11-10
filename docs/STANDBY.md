# Limited emergency chat standby (manual or guarded automatic activation)

This is a **separate, minimal runtime**, not a second fully capable Maxwell.
`MAXWELL_DEPLOYMENT_MODE=standby python bot.py` selects it **before** importing
primary plugins, media handlers, APIs or background workers. The default mode is
`primary` and the primary persona/tools are unchanged. Invalid modes fail closed.

## Status and limits

- Staging the code is not an active backup. Automatic takeover exists only when
  the guarded failover watchdog below is installed **and** armed (`/etc/maxwell-failover/mode`
  contains exactly `auto`). Otherwise activation is manual and fenced.
- Standby answers **explicit mentions in operator-approved guild channels only**.
  No DMs, ordinary channel chatter, primary conversation history, message cache,
  persistent conversation memory, user-install shortcuts or private BYOK settings.
- Before any message text is forwarded to OpenRouter, the individual user must
  explicitly mention Maxwell with `standby-consent`. The initial notice names
  OpenRouter and current-message-only processing. `standby-revoke` clears consent.
  Revocation is unmetered, cancels pending local requests and suppresses future
  responses. Already transmitted data/provider retention cannot be recalled.
  Consent is bounded in memory and resets at process restart; existing primary
  BYOK/provider choices are never silently repurposed as consent. Attachments and
  slash limitation notices do not send message text to the provider.
- No plugin manager, extension loader, command tree, admin/data API, tool dispatch,
  shell, sandbox, generated sites, file/media operations, upload/download, browsing,
  local models or music stack exists in this runtime. Slash interactions receive
  only a fixed ephemeral limitation notice, subject to the same audience controls.
- Attachments/stickers are refused from metadata; their bytes are never downloaded.
  The outbound message adapter accepts only bounded text with mentions disabled.
  Model tool/function calls are rejected rather than executed. Prompt instructions
  and primary dashboard/tool settings cannot enable capabilities.
- Input <=4000 characters; output <=512 model tokens /1800 characters; two model
  calls at most; 35-second provider deadline; 1 MiB response cap; no unbounded queues.
  Five requests/user/five hours in at most 1024 in-memory quota entries. Entries are
  not evicted while current; new identities are refused when full. No persistent
  histories or quota database is copied. Emergency allowance is distinct from the
  primary's allowance, resets on operator restart, and requires an explicitly
  approved restricted audience. It is **not** transparent replication of primary
  premium/privacy/consent/account policy: review that audience before activation.
- Uses direct HTTPS OpenRouter. It refuses main-host/local provider endpoints,
  redirects, and proxy environment settings. Never copy the full primary `.env`.
- The system context truthfully states limited standby, current-message-only chat,
  absent capabilities, and last-known-good config timestamp. It cannot report
  primary health or imply automatic failover. Primary awareness is opt-in via
  `MAXWELL_STANDBY_CONFIGURED=true`, after actual standby provisioning; it explicitly
  says manual-only and does not claim the standby is active. Use
  `MAXWELL_STANDBY_CONFIGURED=staged` while installation/verification is incomplete;
  it explicitly states NOT operationally ready and promises no outage coverage.

## Passive installation

Review and merge the change before promoting it. Known-host and reviewed-launcher assumptions are explicit; this is not an infrastructure fencing guarantee against an independent root operator or replacement orchestration. A disconnected candidate may be
installed for offline checks without credentials or activation permission:

1. Copy a versioned reviewed artifact's `standby/` into a root-owned
   `/opt/maxwell-standby/app/standby/` (no editable source or Git credentials).
2. Create system user/group `maxwell-standby`, no login/home. Create a venv at
   `/opt/maxwell-standby/venv`; install `standby/requirements.txt`.
3. Install `standby/maxwell-standby.service` in `/etc/systemd/system/`. Keep it
   **disabled and stopped**. No Docker daemon/socket, optional packages or API.
4. `/etc/maxwell-standby/` is root-only. Only independently required provider data
   is in `provider.json`: `{base_url,model,api_key}`. Provision `discord-token`
   separately, only after review/approved activation; do not print credentials.
   Use systemd LoadCredential, not environment secrets. Credential files must be
   regular non-symlink files owned by root (or the consuming service user), with
   no group/other permissions, e.g. mode `0600`. The service is non-root, with a
   read-only filesystem, no capabilities, bounded tmpfs/log rate, 512 MiB RAM,
   128 MiB swap and 75% CPU. Root administration API remains separate.
5. Run `python -m standby.runtime --check` offline. This reports capabilities,
   **not** credential readiness or successful gateway login.
6. After review, write the exact approved 40-character SHA to
   `/opt/maxwell-standby/REVIEWED_COMMIT`. Candidate artifacts must not have this
   marker. Set `/etc/maxwell-standby/audience.env` explicitly:
   `MAXWELL_STANDBY_CHANNEL_IDS=123,456`. Apply all primary opt-outs/restrictions
   via a validated config snapshot before accepting this audience.

No activation/token/config means service startup is skipped by systemd conditions;
startup also checks a positive root-provisioned fencing receipt and approved
channel list before importing Discord or contacting its gateway. Channel IDs must
be canonical positive decimals with no blanks; spaced, zero, or empty entries
refuse startup rather than being skipped.

## Scoped one-way configuration snapshots

Run the exporter from the reviewed **primary checkout**, using its shared
`control_defaults.py` (not a copy of the isolated standby artifact):

`python -m standby.sync export --input data/bot_control.json --output /secure/chat-config.json --revision <sha>`

Both authoritative inputs must exist and contain valid JSON:

- `--input` is the primary's `bot_control.json` **object**. Sparse saved overrides,
  including `{}`, are supported: absent supported fields use primary
  `DEFAULT_CONTROL`. Supported boolean values are JSON booleans, integer `0`/`1`,
  or case-insensitive, whitespace-trimmed strings `true`/`false`, `yes`/`no`,
  `on`/`off`, `1`/`0`, normalized with the primary's `parse_bool`. Other values
  (including null) are rejected, not defaulted to enabled.
- The primary's separate `blacklist.json` must be a JSON **array of user IDs**.
  By default it is read beside `--input`; use `--blacklist /actual/data/blacklist.json`
  if it lives elsewhere. An explicitly reviewed empty blacklist is `[]`.
  Missing, unreadable, truncated or malformed input prevents export; do not create
  an empty blacklist just to bypass a failure or discard known restrictions.
- Both inputs must be regular non-symlink files of at most 8 MiB. The exporter
  does not follow links, so a replaced symlink cannot substitute an empty
  blacklist or a weaker control file.

The exporter copies **only** seven supported effective chat controls:
`bot_enabled`, `reply_mentions`, `ignore_users`, `allowed_channels`,
`blocked_channels`, `guild_solo_channel`, and `base_personality` (bounded style).
Primary blacklist entries are merged with `ignore_users`; both restrictions are
enforced after takeover without adding a new snapshot key. The personality comes
from the code-owned primary default, matching the primary loader rather than a
stale persisted override. Lists/mappings must have the expected types and canonical
positive decimal Discord IDs; malformed entries, duplicate JSON object keys,
non-finite JSON numbers, or an oversized merged ignore list reject the whole export.
The same strict parser, including the 8 MiB bound, is used for import, status,
offline preflight, and standby startup. Python last-key-wins is never applied:
a repeated `ignore_users` key cannot drop blacklist entries, and a repeated
provider `base_url` cannot redirect the credential. Unsupported primary settings
are omitted rather than copied.

The runtime honors the snapshot **in addition to**, never instead of, its immutable
emergency audience and text-only restrictions. Primary ignores/blacklist/blocks/solo
channels remain restrictive. DMs are rejected even if their IDs are accidentally
listed in the emergency audience. The style is explicitly labeled untrusted
preference, not permission to override the system or physical runtime boundary.

No `.env`, tokens, provider routes, tools/plugins/commands, file settings, API
permissions, memory, databases, conversation history, BYOK, per-user private
preferences, embeddings, music or payment/account state is exported. Unsupported
primary settings are intentionally not mirrored; this is not full config sync.

Transfer the small snapshot through authenticated HTTPS management. Import **as
root**, never a bot-accessible upload route:

`python -m standby.sync import --input /secure/incoming.json --output /etc/maxwell-standby/chat-config.json`

Imports do **not** fill defaults or normalize primary overrides: every snapshot
must contain all seven controls with strict types (including actual booleans).
Imports enforce schema/type/size/ID/checksum validation, reject unexpected control
keys, partial controls, noncanonical/zero Discord IDs, future timestamps, older
snapshots and same-version conflicts. An existing snapshot with duplicate keys,
or a symlink/non-regular output path, is left untouched rather than interpreted
with last-key-wins. Validated snapshots use a shared nonblocking cross-process
lock across validation/comparison and atomic replacement, plus file/directory
fsync. Post-replacement durability
errors are distinguished from pre-commit rejection; invalid input keeps
last-known-good intact. `standby.sync status --input ...` reports revision,
export time and checksum, without secret/history data.

**Cadence now: manual only**, including immediately before activation. No recurring
primary job, timer, or automatic user-data export is installed by this change.
A future five-minute one-way refresh is a proposal requiring scheduled-export
scope/approval, not functionality claimed here. The runtime loads its credential
snapshot at startup and continues from that last-known-good during an outage;
it never needs main to fetch settings and never pushes standby changes back.
Freshness remains visible, even for old snapshots; age alone does not destroy the
last-known-good config during a long outage. To apply a newer config while live,
use an explicitly approved standby-only stop/start while primary remains fenced.

## Positive fencing, manual takeover and failback

`python -m standby.takeover plan` is read-only and safe to run now.
`activate` and `failback` are **disruptive**, owner-approved operations, not tests.
No live takeover, Discord send, primary stop, or outage rehearsal is implied by
unit tests. Before activation, review/deploy the same guarded commit to primary
and standby and provision the restricted audience/config/Discord credential.

With explicit owner approval to interrupt primary:

`python -m standby.takeover activate --reviewed-commit <sha> --approve-primary-stop`

The operator tool uses the established `zrun`/`brun` admin wrappers, not a public
runtime API. It verifies exact clean reviewed primary revision, supported data
mount/working directory and primary startup fence support. Before touching primary
it checks the offline credential/config/dependency/audience preflight and that no standby process is running. A snapshot older than 15 minutes blocks activation until refreshed. Offline preflight is not proof of live token validity; independently verify live credentials before approving disruption. It then:

1. Exclusively creates a durable primary fence in the shared data directory.
   Guarded primary startup refuses **any** fence, even corrupt content.
2. Disables the known primary container's Docker restart policy and stops it.
3. Positively re-reads stopped state, PID zero, restart disabled and matching fence
   ID, supported entrypoint hash, effective data directory and no nested data mounts. Timeout, stale health, mismatched ID, unknown restart policy or uncertainty
   leaves standby disconnected. Failed operations may leave primary fenced; inspect
   before recovery, never remove fences blindly.
4. Installs a private **boot-local** `/run/maxwell-standby/activation.json`
   receipt and starts standby manually. Systemd copies it into protected service
   credentials, then a privileged pre-start exclusively creates
   `/run/maxwell-standby/owner.json` (single use: a second start fails until the
   ordered failback/stepdown removes it). The source receipt stays in place while
   the unit runs because systemd re-reads credential sources for every Exec
   command; reboot removes all activation sources. The unit
   is **static** (no Install/WantedBy) and does not auto-restart. A second start
   without a newly verified fenced activation cannot log in.

This controls the **known guarded deployment**, not arbitrary root intervention.
Do not independently remove the fence, launch old/unreviewed code, rotate tokens,
create another primary container or run a second orchestrator while standby owns
Discord. Those actions invalidate fencing assumptions. The source guard alone is
not a distributed lease or external infrastructure fence.

With explicit approval to return to primary:

`python -m standby.takeover failback --approve-primary-start`

It stops/disables standby and positively verifies no running PID before matching
and removing all reusable standby activation sources **before** removing the primary
fence, restoring its former restart policy, then starting primary. If primary
restoration fails, standby cannot silently restart from a stale receipt. Separate owner evidence is retained through failures and cleared only after primary is positively running, permitting ordered retry. Reboot loss of /run evidence requires manual reconciliation, not a guessed takeover. If either host
is unreachable or any evidence fails, the tool does not guess or start a competitor.

## Guarded automatic failover (`standby/failover.py`, `standby/primary_gate.py`)

A health timeout alone is still **not** proof that the primary stopped. The
automatic path therefore combines several independent signals, keeps the primary
as the single authority, and closes any overlap as fast as it can be observed.
It cannot provide the guarantee of real power/egress fencing; the residual risk
is listed below.

### Components

| Where | Unit | Privilege | Role |
|---|---|---|---|
| backup | `maxwell-failover.service` | user `maxwell-failover`, no capabilities | probes, decision logic, authenticated HTTPS listener on TCP 8443 (`/failover`: `status`, `heartbeat`, `handback`) |
| backup | `maxwell-failover-actuator.path` + `.service` | root oneshot | re-validates each watchdog request; writes the boot-local `auto_lease` receipt; starts/stops `maxwell-standby.service` |
| primary host | `maxwell-primary-guard.service` | root (needs `docker inspect`) | every 15 s: heartbeat (is the `maxwell` container running/healthy); if the backup holds an automatic lease while the primary bot runs, requests handback immediately |
| primary bot | `bot.py` startup gate | in-process | before Discord login, asks the backup to hand back and waits (max 150 s) for confirmation that its gateway stopped. If the backup is unreachable twice, the primary proceeds. Configured only if `data/standby-failover/client.json` exists (dev has none) |

The failover key is dedicated (not the admin or config-sync key). The client pins
the backup's exact TLS leaf certificate plus IP SAN. No redirects or proxies.

### Takeover (all must hold continuously)

1. No primary heartbeat for `heartbeat_stale_seconds` (60 s; guard pushes every 15 s).
2. Primary unreachable **directly** (TCP 443 and 22 on its IP).
3. The backup's own internet is healthy on every tick: Discord API `GET /gateway`
   returns 200 **and** at least 2 of 4 public anchors (1.1.1.1, 8.8.8.8, 9.9.9.9,
   208.67.222.222 on 443) answer. Any unhealthy tick resets the outage window, so a
   backup-side blip (e.g. a provider switch failure) never triggers takeover.
4. Conditions 1–3 hold for `takeover_after_seconds` (180 s) without interruption.
5. **External confirmation**: independent check-host.net observers in DE/NL/FR/US
   are asked to TCP-probe the primary. Any observer reaching it vetoes takeover
   for 5 minutes (backup↔primary partition, not an outage). An unavailable observer
   service is never treated as evidence; takeover then waits for the extended
   `external_fallback_after_seconds` (15 min) of continuous outage.
6. Not blocked by: manual mode, 10-minute cooldown after a lease ends, a primary
   handback claim within 10 minutes, 1 hour after a split-brain detection, or the
   daily limit (3 automatic takeovers per 24 h, enforced again root-side).

Expected takeover time after a hard primary outage: about 4–5 minutes.

The actuator re-checks arming, that no lease/manual fence exists, that the standby
is stopped, the root-side daily limit and the offline preflight (without the
15-minute snapshot freshness limit: an outage keeps the last-good snapshot). It
then writes an `auto_lease` receipt and starts the unchanged, text-only,
non-root, 512 MiB `maxwell-standby.service`. The standby stays quiet for a 30 s
probation after gateway READY, watching for the primary.

### Handback (any one is enough)

- The primary startup gate or guard sends `handback`: the watchdog requests a
  stop, the actuator stops the unit, verifies inactive with PID 0, removes the
  receipt and lease, and only then confirms. The gate lets the primary log in
  after that confirmation (zero overlap on primary restarts/boots).
- The guard heartbeat reports the primary bot running: immediate stepdown.
- The primary stays directly reachable for `stepdown_after_seconds` (60 s) and
  no heartbeat says its bot is stopped.
- **Discord-side split-brain detector**: the standby sees a message authored by
  its own bot account that it did not send (nonce/ID tracking). That can only be
  the live primary, so it exits (status 3) and the watchdog applies a 1-hour
  cooldown.

If the primary host is up but its bot is not running (guard heartbeat says
stopped), the standby keeps serving until the primary's startup gate claims.

### Residual risk (why this is not a fencing guarantee)

- A primary that can reach Discord but is unreachable from the backup **and**
  from all external observers **and** cannot reach the backup for 4+ minutes can
  overlap with the standby. The overlap ends when the primary answers someone in
  a channel the standby sees (detector), or when connectivity returns.
- If the backup is unreachable when the primary starts while a lease is active,
  the primary proceeds after about 10 s; overlap lasts until the backup's next
  tick sees the primary (≤ 60–75 s) or the detector fires.
- During any overlap both bots may answer the same mention. The standby is
  text-only, so it cannot run tools, files or commands twice.
- check-host.net is a third party; it can only *veto* or delay takeover.

### Operations

- Status: `cat /run/maxwell-failover-watchdog/status.json` (backup),
  `cat /run/maxwell-primary-guard/status.json` (primary).
- **Disarm** (no new automatic takeovers; stepdowns still work):
  `echo manual > /etc/maxwell-failover/mode`. Re-arm: `echo auto > /etc/maxwell-failover/mode`.
- Stop everything automatic: `systemctl disable --now maxwell-failover.service maxwell-failover-actuator.path`.
  The primary gate stays harmless (backup unreachable => proceeds after ~10 s);
  remove `data/standby-failover/` on the primary to disable it entirely.
- Force handback now (from the primary): `python3 /opt/maxwell-failover/primary_gate.py gate-test --data-dir /root/maxwell/data`.
- Live simulation without actuation (backup): `python -m standby.failover simulate --main-ip 192.0.2.1 --armed`.
- Bounded observe-only gateway test that can never send (backup, root):
  `python -m standby.failover observe-test --seconds 60`.
- Manual fenced takeover/failback (`standby.takeover`) is unchanged and is never
  touched by the automatic path: the actuator refuses to stop a manually fenced
  standby, and the watchdog does not manage it.

## Verification / rollback

Offline regression tests cover immutable policy, real message/interaction handlers
with fake Discord/provider, attachment/model-tool-call rejection, guild-only
audience enforcement, absent primary imports, bounded quota, sparse primary CLI
export/import/status, effective default/boolean normalization, merged authoritative
blacklists, rejection of missing/malformed/symlink/oversized restrictions, strict
snapshot imports, duplicate-key refusal at runtime/preflight/activation, mistyped
controls denied by chat policy, private credential permissions, validated atomic
config sync, approval gates, positive fencing, timeout/stale-proof rejection, and
ordered failback. No live Discord sends.
Before promotion, get review and test deployment-specific behavior independently.

Passive rollback: keep/remove the stopped disabled unit and candidate directory;
remove staged provider/config credentials only when abandoning standby. Do not
revoke shared provider credentials without coordinating primary. During an active
takeover, rollback is the approved ordered failback, never starting both gateways.
The independent HTTPS administration service is unrelated; leave it intact.
