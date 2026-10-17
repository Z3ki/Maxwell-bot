# Premium grants and bounded task workers

Premium can be granted to any Discord user ID by a configured application owner:
`/config → Bot controls → Grant / revoke Premium`. Enter the ID and `premium` or
`free`. The modal and submission both verify the owner and active private panel.
Grants persist in the quota database across restarts. Premium currently provides
the 2,000-message allowance (when quota enforcement is enabled), optional
`AI_PREMIUM_MODEL` routing and the `delegate_task` tool. An existing custom message
limit still wins. Granting a plan does not take payment or start a subscription.
Paid checkout remains disabled pending actual billing terms and operator review.

For Premium requests Maxwell acts as the coordinator: website creation/edits,
code and shell work, research, repository tasks, media analysis and other
substantial investigations go through `delegate_task`. Those task tools are
removed from the coordinator catalog and direct calls are rejected by the
execution gate. Ordinary conversation stays immediate. Free users and an
operator-disabled worker feature retain the existing direct-tool path.

One or two workers receive concrete task requirements and their own tool results,
not the full conversation. `general` can combine the reviewed task tool catalog:
shell, website tools, web search/fetch, GitHub repository work, media inspection,
and scoped Discord lookup/search tools. Narrower shell/website/research kinds and
explicit tool lists remain available. Media requires a capable selected model.
Discord communication, moderation, settings, credentials and consent remain on the
coordinator; workers cannot recursively delegate, send Discord messages or export
shell files. The effective tool set intersects task/profile permissions and live
caller authorization. Premium never bypasses the sandbox, site ownership,
plugin/server restrictions, private visibility, web-off, deletion suppression or
the personal-provider tool-call budget shared by siblings within each job.

Worker provider precedence is: **dedicated worker BYOK → personal chat BYOK →
operator `AI_WORKER_*` route → primary chat provider**. A saved but unavailable key
stops the task; it never silently falls through to another account. Configure a
separate key/model privately at `/config → More options → Subagent AI provider`,
or choose **Subagents (Premium)** in the dashboard AI connection selector. Chat
and worker keys/settings are independent, encrypted with the same operator master
key and both removed by full deletion. A user can review/delete saved keys after
Premium revocation, but worker execution still requires Premium.

`delegate_task` returns a job ID immediately. Maxwell can answer more messages
in the same channel while workers continue; an unrelated chat turn, its deadline
or its cancellation does not own the job. `manage_tasks` lets Maxwell check status
or cancel a task when asked, scoped to that user and the current channel/visibility.
The prompt includes a small status summary, not the task transcript. Do not poll
or repeat progress unasked. One completed job produces one factual completion
notification in its original context, with mentions disabled.

`/tasks` privately lists your own jobs across contexts, with refresh/cancel controls
and an optional job ID. `/cancel` cancels your current chat request and your worker
jobs in that conversation. Ask Maxwell to cancel a job conversationally to keep
chatting without it. Cancellation joins children and closes their owned clients;
already completed writes can remain. Main chat never borrows a worker-owned key
or waits for that client's shutdown. Workers open their own personal connection
instead of borrowing the chat turn's short-lived BYOK client.

For application commands, completion keeps the original Public/Private visibility
and uses that interaction's follow-up token while valid. Discord tokens expire
after 15 minutes and user-only installations have a follow-up cap; see the
[official interaction reference](https://github.com/discord/discord-api-docs/blob/main/developers/interactions/receiving-and-responding.mdx).
An expired/failed follow-up is recorded, without sending a private result publicly,
starting a DM or retrying an ambiguous send. Use a fresh `/tasks job_id:...` to
retrieve the report privately. Ordinary message/DM tasks reply to the original
channel; this never starts an unsolicited DM. Completion delivery is bounded to
10 seconds and claimed durably before HTTP to avoid duplicate notifications.

Worker transcripts and task text exist only in memory while the job runs. Final
reports remain in a bounded in-memory cache for up to one hour (at most 256 jobs,
earlier eviction is possible under load), so the user can retrieve results even
if delivery fails. Source-message/attachment references are released on completion.
No task text, transcript, report, webhook token or API key is saved in the job DB.
`plugins/maxwell_extras/task_jobs.sqlite3` stores job/user/source/channel/guild IDs,
visibility, task kinds, lifecycle timestamps, state, side-effect/delivery markers
and tool counts. Metadata defaults to one day of retention, configurable with
`MAXWELL_TASK_METADATA_RETENTION_DAYS`; periodic cleanup and full deletion remove it.

A restart marks previously live jobs interrupted and ambiguous deliveries unknown;
it never resumes or replays their side effects. Detailed reports do not survive
restart. Plugin unload/shutdown cancels and joins jobs before provider teardown;
maintenance drain includes them. Full deletion cancels live jobs where possible,
removes metadata and cached results, and prevents old writes from recreating rows.
The bot observes dashboard deletion through shared metadata/epoch checks. Essential
processing can already have reached a provider; published websites and sandbox
files retain their separate tool/operator deletion lifecycle.

Defaults: one job per user, at most two siblings, four active worker slots globally,
twelve LLM steps each, a 128,000-character text budget and a 600-second job engine
deadline. Workers have independent request/job deadlines; chat keeps its own
normal deadline. Sibling tool calls serialize; model calls may overlap. Controls:
`premium_task_workers_enabled`, `task_workers_max_active` (1–8),
`task_workers_max_steps` (1–32), `task_workers_timeout_seconds` (10–7200) and
`task_workers_context_chars` (up to 2,000,000). Permission, Premium, privacy and
source-access checks run before model/tool steps; a two-second watchdog also
stops long in-flight work when access is revoked. A request already sent externally
may finish before cancellation takes effect. Reports distinguish incomplete,
failed, stopped, cancelled and interrupted jobs; a model report is not independent
verification. Inspect partial changes before retrying.

For slow models, configure the independent worker LLM and whole-task deadlines in
[PROVIDERS.md](PROVIDERS.md). A personal worker deadline is capped by the operator
worker ceiling; raising request timeout alone does not raise that ceiling. Real Discord/provider/sandbox deployment validation remains
an operator task; automated tests use controlled providers and tools.

## Custom agents and Maxwell-selected permissions

Open `/config → More options → Custom agents → Manage agent`, or ask Maxwell to
save an agent with `manage_task_agent`. A profile has a lowercase ID, instructions
and a shell/website/research/general/custom scope. Maxwell can choose and update explicit
tool permissions, and can narrow them for an individual `delegate_task` call.
General/custom scopes combine the reviewed task tools described above. The effective permissions intersect
the profile, task and caller's currently enabled tools. Permission removal is
checked again immediately before execution; adding permissions affects subsequent
requests. This does not change Discord roles, application-owner access, Premium
grants, server controls or the underlying sandbox.

Example request: “Save a custom agent named reviewer that uses shell and edit_site
to test accessibility, then ask reviewer to inspect my website.” Saved instructions
are optional feature settings, separate from conversation history. Up to ten
profiles are retained per user, with a default 90-day expiry after the last edit
(`MAXWELL_AGENT_PROFILE_RETENTION_DAYS`). Cleanup runs with the privacy lifecycle.
Profiles live in `plugins/maxwell_extras/task_agents.sqlite3`; full deletion removes
them and blocks writes from older requests. A user whose Premium was revoked can
still review/delete their profiles. A transient custom worker needs no saved
profile. Model steps use the independently selected worker route and its context,
reasoning, output and deadline settings. Profiles contain no API keys; those live
in the encrypted connection vault. Older profiles keep their saved tool lists.

GitHub account authorization and repository policy changes remain direct coordinator actions. Workers cannot modify account tokens or widen repository policy. Applicable tool-specific instructions (including site backend and repository verification rules) travel with the worker task; conversation history and unrelated personality prompts do not.
