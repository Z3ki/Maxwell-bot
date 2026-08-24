# Memory, events, attention, and approvals

This branch adds small, dependency-free primitives that can be composed by Maxwell's existing tool registry.

- `memory_controls.py` exposes `save_memory`, `update_memory`, and `forget_memory`. Records have explicit `profile`, `project`, `episodic`, or `transcript` layers. `parse_memory_command` handles deterministic `remember ...` and `forget ...` commands before RAG/REM processing.
- `event_dispatch.py` provides typed `Event` values, wildcard subscriptions, and isolated prompt context. Cron, webhook, and Discord adapters can dispatch into the same path without polling.
- `attention.py` prevents unsolicited messages unless they are actionable, sufficiently urgent/valuable, outside quiet hours, and past cooldown; presence is required. Critical urgency can break quiet hours.
- `approval.py` implements expiring, requester-bound, single-use draft/approve/consume gates for moderation, shell, messaging, and other destructive tools.

Adapters should call these modules from the existing registry rather than bypassing them. Event payloads remain data under an `event` context key and must not become system instructions.
