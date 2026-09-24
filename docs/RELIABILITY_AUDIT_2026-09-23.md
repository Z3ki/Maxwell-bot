# Maxwell reliability audit — 2026-09-23

## Scope and architecture

This change is an incremental reliability repair. It does **not** certify
Maxwell for commercial deployment. The repository has 354 tracked files at
inspection, including an 837 KB `bot.py`, a 193 KB `autonomy.py`, a 147 KB
`providers.py`, a 144 KB `rag_memory.py`, 25 plugin directories, and over 100
test modules. A complete behavioral audit and safe removal of every legacy
agent implementation needs additional reviewable changes.

| Boundary | Current implementation | Disposition |
| --- | --- | --- |
| Discord ingress, reply queue, request journal | `bot.py`, `message_pipeline.py` | Essential; retain and test cancellation/recovery with a test Discord app. |
| AI calls and fallback | `providers.py`, `bot.py` | Essential; paid-provider failure scenarios remain untested here. |
| Prompt composition | `bot.py` `TOOL_PROTOCOL`, `maxwell_core/prompts` | Essential; removed forced worker delegation, but the central prompt needs a separate consolidation. |
| Tools and plugins | `maxwell_core/tools`, `maxwell_core/plugins`, `plugins/*` | Essential; chess and owner shell fixes here. Other schemas need execution-level review. |
| Chess state | `chess_game.py`, `plugins/chess`, `tooling/helpers.py` | Repaired and tested through the actual request dispatcher. |
| Detached AI workers | `jobs.py`, `plugins/background_jobs`, `plugins/agent_life` | Retired from default plugin loading; `,bg` no longer starts workers. Legacy source and stored job records remain for migration and historical test coverage. |
| Ordinary asynchronous maintenance | queue, memory embedding, cleanup, plugin event tasks | Retained; these are not delegated LLM assistants. |
| Autonomy and REM | `autonomy.py`, `rem.py`, `bot.py` | Separate opt-in engines; further audit needed before claiming single-assistant production behavior. `ENABLE_AUTONOMY=false` in the sample env. |
| Dashboard and site backend | `site_server.py`, `site_backend.py`, `api/*` | Security and authenticated live deployment checks outstanding. The old `SiteTestTool` in `tooling/helpers.py` is not registered by the sites plugin; `site_test.py` and its browser tests are leftover helper coverage. |
| Persistence and deployment | JSON stores, Docker Compose host networking | Recovery partially covered by tests; production backup and restart drills outstanding. |

## Reproduced defects and corrections

1. `plugins/chess/impl.py` copied names from `tooling.helpers` but skipped all
   names starting with `__`. The tools then read `__CHESS_IMPORTED__`, causing
   the production `NameError`. Import that symbol explicitly. Limit the
   optional dependency handler to missing `chess`, so unrelated import bugs
   cannot masquerade as an optional dependency failure.
2. A malformed `depth` raised before game creation. Reject it with a useful
   error, and leave the channel free.
3. A completed chess game remained in the manager and blocked the next game.
   Allow a new game to replace an ended game, while still rejecting a second
   active game in the same channel.
4. Persisted `plugins.json` could re-enable detached agents. Skip setup of
   the two legacy agent plugins even if their old state says enabled; loading
   `agent_life` otherwise schedules periodic work at startup. `,bg` now gives
   a clear retired-command response. Saved jobs and code are left intact for
   migration; ordinary asynchronous tasks continue.
5. `shell` was advertised and dispatched to ordinary Discord users. Mark it
   admin-only for catalog filtering and enforce configured owner ID membership
   at the dispatch boundary. This is only one part of the security review:
   Docker socket access and other privileged paths remain open issues.

## Verification boundaries

`tests/test_chess_runtime.py` drives real chess plugin instances through
`MaxwellBot._execute_tool_by_name`, using fake Discord I/O and an isolated
temporary chess store. It covers image posting, starting and restarting,
legal and illegal moves, separate channels, stored game recovery, player
ownership, resignation, bad arguments, and dependency unavailability. The
older `tests/test_chess.py` covers board rendering, move search, and manager
logic. The retired-plugin regression asserts that persisted enabled state
cannot initialize those plugins. The shell regression asserts denied calls
never execute and an owner call can execute.

The test suite uses mocked Discord, model providers, and network boundaries.
One optional test of the leftover site probe skips without Chromium; Chrome
is not required by the current tool registry or bot runtime.
No production token, payment credential, or customer data was used. There was
no real Discord end-to-end session, no paid API test, no Docker restart drill,
no load test under concurrent real users, and no full dashboard penetration
test. A single local pytest process measures test-harness cost, not VPS
request latency or commercial capacity.

## Release blockers and reproduction

- The public host-network Compose file mounts `/var/run/docker.sock` into the
  bot container. `:ro` does not limit Docker API calls over a Unix socket.
  A compromise of this process may control the host. Remove this mount or
  isolate Docker operations behind a narrowly scoped service before selling.
- The repository still has a large prompt and multiple historical AI paths;
  test tool iteration deadlines, provider streaming, request cancellation,
  duplicated side effects, and prompt injection with a separate development
  Discord application. The current maximums include hour-scale timeouts.
- The dashboard, site APIs, moderation actions, plugin install/reload, and
  owner administration need authorization review under multiple test users.
- A checkout without `python-chess` should simply omit chess registration.
  To reproduce the original crash on the parent commit, call `chess_start`
  through `MaxwellBot._execute_tool_by_name` with python-chess installed.
- Reproduce the remaining operational risk with an isolated dev bot: send
  simultaneous tool-calling messages in two channels; kill the provider during
  a tool result; restart mid-request; verify exactly one terminal outcome and
  no repeated external side effect. This has not been performed here.

## Development and release workflow

Keep `main` as default/production. Create changes on `feature/*` or `fix/*`
from `dev`, open a PR targeting `dev`, and run CI and isolated integration
tests before merging. CI now runs on pushes to `dev` too. There is no change to
production startup or deployment configuration in this PR. Use a separate
development Discord app, credentials, data directory and payment test mode.
For `main`, recommend a GitHub ruleset requiring a `dev`-to-`main` PR, passing
Python 3.11 and 3.12 CI, one approval, no force push, and no deletion. Rulesets
were not changed through this repository checkout.

## Maxwell Plus, after reliability and security gates

Customer-facing AI usage is messages, not tokens. The free allowance is 100
messages per rolling five-hour window. Personal Plus is proposed at
$2.99/month per user and would raise that individual allowance; the higher
amount is not decided. Server Plus is proposed at $4.99/month per server and
would use Discord's native Guild Subscription, remaining associated with the
purchased server. It is not transferable. Its shared allowance and per-user
fair-use cap are not decided. Do not build a subscription-transfer flow.

Token and API-cost accounting stay internal for spending protection. Premium
is not launching: do not activate billing, paid restrictions, checkout, or
promotional announcements. `/premium` is an optional discovery command, with
a quiet line in `/help` and `/usage` only. No live payment path is implemented
or enabled.

When billing is eventually chosen, verify provider signatures, deduplicate
event IDs, and reconcile renewals, failures, refunds, and revocation. Do not
test or implement transfer of a server subscription. Check Discord developer
onboarding eligibility for Puerto Rico before choosing a billing provider.
Discord describes a 15% Growth Tier platform fee plus applicable fees and
requires supported paid features to be available through its own Premium Apps
in supported regions. Stripe says businesses in Puerto Rico can register as
US businesses.

Official references:

- https://support-dev.discord.com/hc/en-us/articles/17297949965079-How-Do-I-Monetize-My-App
- https://support-dev.discord.com/hc/en-us/articles/17299902720919-Premium-Apps-Payout
- https://support-dev.discord.com/hc/en-us/articles/23810643331735-Premium-Apps-Required-Support-for-Monetizing-Apps
- https://support.stripe.com/questions/stripe-availability-for-outlying-territories-of-supported-countries
- https://docs.stripe.com/billing/subscriptions/webhooks
- https://ai.google.dev/gemini-api/terms

Use a licensed commercial API with explicit cost ceilings; personal AI Pro,
ChatGPT Pro, SuperGrok, and unofficial OAuth connections are not an acceptable
assumed commercial inference budget. Price and provider decisions require
current usage measurements and a tested budget limiter. Do not charge anyone
or deploy this branch until the blockers above are addressed.
