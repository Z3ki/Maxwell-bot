# Premium grants and bounded task workers

Premium can be granted to any Discord user ID by a configured application owner:
`/config → Bot controls → Grant / revoke Premium`. Enter the ID and `premium` or
`free`. The modal and submission both verify the owner and active private panel.
Grants persist in the quota database across restarts. Premium currently provides
the 2,000-message allowance (when quota enforcement is enabled), optional
`AI_PREMIUM_MODEL` routing and the `delegate_task` tool. An existing custom message
limit still wins. Granting a plan does not take payment or start a subscription.
Paid checkout remains disabled pending actual billing terms and operator review.

Ask Maxwell to delegate a specific shell, website or research task. It may start
one or two child workers within the current request. Each receives only the task
text and its own tool results, using the same selected provider/BYOK connection,
reasoning settings and time limits as the parent. Do not put credentials in task
text. A worker cannot recursively delegate, message Discord, export shell files,
change settings or access tools outside its family. Premium grants do not bypass
shell isolation, site ownership, plugin/server restrictions, private visibility,
web-off settings, deletion suppression or the shared BYOK tool budget.

Workers keep transcripts in memory only. No jobs survive cancellation or restart;
there is no autonomous scheduling, cross-user context or saved worker memory.
Generated site/files and shell workspace data follow those tools' existing
retention and manual deletion rules. The selected AI provider receives task text
and tool results; disabling history does not stop that processing.

Defaults: one delegation per user at a time, at most two siblings, four active
workers globally, six LLM steps per worker and a 600-second worker deadline within
the whole-request deadline. Sibling tool calls serialize to reduce write races;
model calls may overlap. The operator may change `premium_task_workers_enabled`,
`task_workers_max_active` (1–8), `task_workers_max_steps` (1–12) and
`task_workers_timeout_seconds` (1–7200) in `bot_control.json`. Old removed
`subagent_*` controls cannot resurrect the retired runtime. Workers recheck plans
and privacy state before each model/tool step. Revocation stops further steps;
an already-running external request may finish before that check.

Reports distinguish unavailable tools, capacity, context/step/time limits and
failures. A model report is not independent verification. Completed actions can
remain after failure/cancel; inspect them before retrying. Workers stop when the
context budget is full rather than forgetting earlier writes and repeating them.
The normal `/cancel` cancels the parent and its children. Shell cancellation uses
the existing sandbox teardown; this feature adds no host shell access.

For slow models, configure both LLM and whole-task deadlines in
[PROVIDERS.md](PROVIDERS.md). A 180-second whole-task deadline will still cancel a
600-second LLM call. Real Discord/provider/sandbox deployment validation remains
an operator task; automated tests use controlled providers and tools.

## Custom agents and Maxwell-selected permissions

Open `/config → More options → Custom agents → Manage agent`, or ask Maxwell to
save an agent with `manage_task_agent`. A profile has a lowercase ID, instructions
and a shell/website/research/custom scope. Maxwell can choose and update explicit
tool permissions, and can narrow them for an individual `delegate_task` call.
Custom scopes can combine `shell`, `create_site`, `edit_site`, `list_sites`,
`site_server`, `web_search` and `fetch_url`. The effective permissions intersect
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
profile. Model steps use the current selected provider and existing context,
reasoning, output and deadline settings; profiles contain no separate API keys.
