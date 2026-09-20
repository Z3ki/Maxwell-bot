# GitHub Projects autonomy

`plugins/github_projects` adds per-user persistent Docker workspaces, policy-controlled GitHub identities, signed webhook wakeups, polling fallback, recurring maintenance goals, PR review/merge, issue replies, verification, and a durable project knowledge ledger.

Configure `MAXWELL_GITHUB_TOKEN` for Maxwell's shared identity (admin-only policy assignment), `MAXWELL_GITHUB_USER_TOKEN_<DISCORD_USER_ID>` for per-user identities, and `MAXWELL_GITHUB_WEBHOOK_SECRET` for `POST /api/github/webhook`. Use fine-grained tokens with minimum repo permissions.

Normal project shell commands never receive GitHub credentials. Credentialed git runs in short-lived sidecar containers; the persistent project shell sees only that user's workspace. `mode=read|write|admin` gates side effects. `auto_review`, `auto_merge`, and `auto_issue_reply` enable autonomous maintenance. `schedule_set` creates cron-like recurring goals (minimum 5 minutes).

Coding workflow: `policy_set` → `checkout` → inspect/edit/test with `run` → `verify` → inspect `diff` → `commit`/`push`/`pr_create` when policy allows. PR review uses `pr_get`, `pr_diff`, optionally `pr_checkout`, local verification, then `review`; merge additionally requires `mode=admin`.
