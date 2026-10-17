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
the shared personal-provider tool-call budget.

Worker provider precedence is: **dedicated worker BYOK → personal chat BYOK →
operator `AI_WORKER_*` route → primary chat provider**. A saved but unavailable key
stops the task; it never silently falls through to another account. Configure a
separate key/model privately at `/config → More options → Subagent AI provider`,
or choose **Subagents (Premium)** in the dashboard AI connection selector. Chat
and worker keys/settings are independent, encrypted with the same operator master
key and both removed by full deletion. A user can review/delete saved keys after
Premium revocation, but worker execution still requires Premium.

Workers keep transcripts in memory only. No jobs survive cancellation or restart;
there is no autonomous scheduling, cross-user context or saved worker memory.
Generated site/files and shell workspace data follow those tools' existing
retention and manual deletion rules. The selected AI provider receives task text
and tool results; disabling history does not stop that processing.

Defaults: one delegation per user at a time, at most two siblings, four active
workers globally, twelve LLM steps per worker, a 128,000-character text context budget
and a 600-second worker deadline. Premium whole requests allow at least that
ceiling plus 30 seconds for the coordinator report. Sibling tool calls serialize to reduce write races;
model calls may overlap. The operator may change `premium_task_workers_enabled`,
`task_workers_max_active` (1–8), `task_workers_max_steps` (1–32) and
`task_workers_timeout_seconds` (10–7200), and `task_workers_context_chars` (up to 2,000,000) in `bot_control.json`. Old removed
`subagent_*` controls cannot resurrect the retired runtime. Workers recheck plans
and privacy state before each model/tool step. Revocation stops further steps;
an already-running external request may finish before that check.

Reports distinguish unavailable tools, capacity, context/step/time limits and
failures. A model report is not independent verification. Completed actions can
remain after failure/cancel; inspect them before retrying. Workers stop when the
context budget is full rather than forgetting earlier writes and repeating them.
The normal `/cancel` cancels the parent and its children. Shell cancellation uses
the existing sandbox teardown; this feature adds no host shell access.

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
