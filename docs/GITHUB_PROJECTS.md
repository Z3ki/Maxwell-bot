# GitHub Projects

`plugins/github_projects` adds per-user persistent Docker workspaces, policy-controlled GitHub identities, PR review/merge, issue replies, verification, and a durable project knowledge ledger.

Users can authenticate through Maxwell: `github_repo action=auth` (optional `scopes=repo,workflow`) generates a GitHub login link for that Discord user. After authorization, the token is stored under `data/plugins/github_projects/user_tokens.json` and never printed. Configure one OAuth App with `MAXWELL_GITHUB_OAUTH_CLIENT_ID` / `MAXWELL_GITHUB_OAUTH_CLIENT_SECRET` and `MAXWELL_PUBLIC_BASE_URL`; the default callback is `https://<public>/api/github/oauth/callback`, or set `MAXWELL_GITHUB_OAUTH_REDIRECT_URI` to override it. The public base URL remains required even with an override. `auth_set` also accepts a PAT, and `auth_clear` removes the stored token. An operator-provided `MAXWELL_GITHUB_USER_TOKEN_<discord_user_id>` takes precedence over the stored token and is not removed by `auth_clear`. `MAXWELL_GITHUB_TOKEN` is the optional shared `identity=bot` identity.

Bot identity policies can be selected only by a Maxwell admin. GitHub API and
service Git actions recheck the requesting user's current admin status before
using the shared bot token, including when an existing policy selects it.

Normal project shell commands never receive GitHub credentials. Service Git runs only structured operations in short-lived sidecars with a private Git metadata snapshot. It ignores workspace hooks, filters, fsmonitor, includes, signing programs, credential helpers, and remote overrides. Network operations pin `https://github.com/owner/name.git`, disable redirects, and use a private URL-scoped authentication file populated over stdin; tokens never appear in Docker arguments, environment, or published metadata. GitHub Git transport uses HTTP Basic authentication, while API requests use Bearer authentication. The persistent project shell sees only that user's workspace. `mode=read|write|admin` gates the structured write/admin actions; it does not make an opted-in custom shell command read-only.

Repository operations run only in response to a user request. Scheduled maintenance, webhook wakeups and repository polling have been removed. Old automatic policy fields are ignored and removed when a policy is saved.

Repository code execution requires an explicit Boolean `allow_security_testing=true`
in that user's policy for that repository. This includes custom `run` commands,
`verify` with a custom test command, and Git checkout/sync/PR checkout/commit/push.
A shell blacklist cannot reliably distinguish development commands from security
testing; custom commands therefore opt into arbitrary project code execution.
Service Git retains the same admission gate but never runs repository hooks or
executable configuration. Missing, false, or malformed values deny execution
before a workspace process starts.

| Operation | Execution permission |
|---|---|
| `status`, `diff`, `verify` without `command` | Available without the opt-in; use isolated read-only inspection |
| `run`, custom `verify`, checkout/sync/PR checkout, commit/push | Require `allow_security_testing=true`; existing write/admin checks also apply |
| GitHub API inspection, reviews, issues, and merges | Existing repository identity and read/write/admin policy apply |

Set the flag through `github_repo action=policy_set repo=owner/name
allow_security_testing=true` before checking out or running code. The flag controls
admission for that user and repository. Changing it affects later requests and
does not terminate a command already admitted. Opted-in commands retain that
user's existing workspace access.

Built-in local inspection starts a disposable container with no network, an
immutable root filesystem, and only the target checkout mounted read-only. A
private Git metadata snapshot ignores source configuration, hooks, includes,
filters, and text conversion. Inspection compares raw worktree contents; projects
using Git filters can use an opted-in custom command for their normal Git view.
Linked worktrees are reported as unsupported by this read-only inspector.

Service Git publishes only ordinary object/ref/index state, preserving existing
workspace configuration without using it. Direct `user.name` and `user.email`
values are copied as data; missing identities use Maxwell's default. Linked Git
directories, symlinked Git metadata, and reftable repositories fail closed.
Alternate object stores are not imported. Metadata snapshots are bounded to
4 GiB and 250,000 entries.

Token saves and logout share one cross-process file lock across the bot and OAuth
API. Async mutations run outside the Discord event loop, and repeated logout
does not rewrite another user's credentials.

Coding workflow: `policy_set` → `checkout` → inspect/edit/test with `run` → `verify` → inspect `diff` → `commit`/`push`/`pr_create` when policy allows. Commit with `github_repo action=commit repo=owner/name commit_message="Describe the change"`; `message` is reserved for the Discord invocation context. Reviews and issue replies use `body`. PR review uses `pr_get`, `pr_diff`, optionally `pr_checkout`, local verification, then `review`; merge additionally requires `mode=admin`.
