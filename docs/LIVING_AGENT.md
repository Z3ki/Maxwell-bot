# Agent Life: persistent autonomy and per-user YOLO sandboxes

`agent_life` adds a durable activity layer on top of Maxwell's existing
AutonomyEngine and background workers.

## Per-user sandbox

`user_sandbox` creates one persistent Docker container + workspace mount per
Discord user. Maxwell can run arbitrary development commands there, install
apt/pip/npm dependencies, compile, test, run local services, and iterate
without per-command approval. This is intentionally a **YOLO zone inside the
sandbox**.

The boundary is still strict: no Docker socket is mounted, no host secrets are
injected, and no other user's workspace is mounted. The container has CPU,
memory and PID limits plus `no-new-privileges` and a reduced capability set.

Examples:

- `user_sandbox(action="exec", command="git clone … && cd app && pytest")`
- `user_sandbox(action="install", manager="apt", packages="clang lldb")`
- `user_sandbox(action="exec", workdir="myproject", command="npm test")`

## Life tasks

`agent_life` supports three durable task types:

- `remind`: deterministic user reminder, one-shot or recurring.
- `task_set`: recurring/one-shot autonomous work on a project, site, MCP,
  GitHub repo, or research target. Each due run launches a detached worker.
- `ambient_set`: periodically wakes the normal AutonomyEngine. It does **not**
  bypass social/floor/duplicate gates; it only gives Maxwell another chance to
  inspect the room and decide whether speaking is useful.

A work task is pinned to its owner and origin channel. Background jobs inherit
that user identity so per-user tools (GitHub workspaces, sandbox, memories)
remain correctly isolated.

Example intents:

- "Every 2 hours continue cleaning up repo X, run tests, and open a PR when a
  useful unit is complete."
- "Once a day check the project's MCP source and update our local artifact if
  anything actionable changed."
- "Keep working on the site until the acceptance checklist passes."
- "Remind me in 40 minutes to check the deploy."

External website, issue, PR and MCP content remains untrusted input; it cannot
expand the task's authority or override the user's scope.
