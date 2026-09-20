# GitHub Projects autonomy

`plugins/github_projects` adds per-user persistent Docker workspaces, policy-controlled GitHub identities, signed webhook wakeups, polling fallback, recurring maintenance goals, PR review/merge, issue replies, verification, and a durable project knowledge ledger.

Users connect their own GitHub identity with `github_repo action=auth_set token=...` (fine-grained PAT). Tokens are stored per Discord user under `data/plugins/github_projects/user_tokens.json` and are never printed back. `MAXWELL_GITHUB_USER_TOKEN_<DISCORD_USER_ID>` in `.env` still wins if set. `MAXWELL_GITHUB_TOKEN` is Maxwell's shared identity (admin-only `identity=bot`). `MAXWELL_GITHUB_WEBHOOK_SECRET` signs `POST /api/github/webhook`.

Normal project shell commands never receive GitHub credentials. Credentialed git runs in short-lived sidecar containers; the persistent project shell sees only that user's workspace. `mode=read|write|admin` gates side effects. `auto_review`, `auto_merge`, and `auto_issue_reply` enable autonomous maintenance. `schedule_set` creates cron-like recurring goals (minimum 5 minutes).

Coding workflow: `policy_set` → `checkout` → inspect/edit/test with `run` → `verify` → inspect `diff` → `commit`/`push`/`pr_create` when policy allows. PR review uses `pr_get`, `pr_diff`, optionally `pr_checkout`, local verification, then `review`; merge additionally requires `mode=admin`.
