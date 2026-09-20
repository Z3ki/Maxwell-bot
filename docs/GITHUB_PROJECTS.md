# GitHub Projects autonomy

`plugins/github_projects` adds per-user persistent Docker workspaces, policy-controlled GitHub identities, signed webhook wakeups, polling fallback, recurring maintenance goals, PR review/merge, issue replies, verification, and a durable project knowledge ledger.

Everyone logs in through Maxwell. `github_repo action=auth` (optional `scopes=repo,workflow`) generates a GitHub login link for that Discord user. After they authorize, the token is stored under `data/plugins/github_projects/user_tokens.json` and never printed. Configure one OAuth App with `MAXWELL_GITHUB_OAUTH_CLIENT_ID` / `MAXWELL_GITHUB_OAUTH_CLIENT_SECRET` and `MAXWELL_PUBLIC_BASE_URL` so the callback is `https://<public>/api/github/oauth/callback`. `MAXWELL_GITHUB_TOKEN` remains an optional admin-only `identity=bot` identity. `MAXWELL_GITHUB_WEBHOOK_SECRET` signs `POST /api/github/webhook`.

Normal project shell commands never receive GitHub credentials. Credentialed git runs in short-lived sidecar containers; the persistent project shell sees only that user's workspace. `mode=read|write|admin` gates side effects. `auto_review`, `auto_merge`, and `auto_issue_reply` enable autonomous maintenance. `schedule_set` creates cron-like recurring goals (minimum 5 minutes).

Coding workflow: `policy_set` → `checkout` → inspect/edit/test with `run` → `verify` → inspect `diff` → `commit`/`push`/`pr_create` when policy allows. PR review uses `pr_get`, `pr_diff`, optionally `pr_checkout`, local verification, then `review`; merge additionally requires `mode=admin`.
