"""Per-user persistent storage: /storage in the shell, a public folder, a tool."""

from __future__ import annotations

import logging
import time

from maxwell_core.prompts.component import PromptComponent

logger = logging.getLogger(__name__)

# Rendered on every turn that has shell or storage; walking a 2000-file tree
# per turn for a status line is how you end up profiling prompt assembly.
_STATUS_TTL = 30.0


def setup(bot, ctx):
    from .store import MB, UserStorage
    from .tool import StorageTool
    from .urls import public_root

    def plan_lookup(uid: str) -> str:
        # Looked up per call: an operator grant or revoke applies immediately.
        ledger = getattr(bot, "_message_quota", None)
        return ledger.plan_for(uid) if ledger is not None else "free"

    storage = UserStorage(ctx.data_dir, plan_lookup=plan_lookup)
    bot._user_storage = storage
    ctx.register_service("user_storage", storage)
    cache: dict[str, tuple[float, str]] = {}

    def render(request) -> str:
        uid = str(request.user_id or "")
        if not uid.isdigit():
            return ""
        hit = cache.get(uid)
        if hit and time.monotonic() - hit[0] < _STATUS_TTL:
            return hit[1]
        try:
            quota = storage.quota(uid)
            if quota <= 0:
                text = ""
            else:
                used, files = storage.usage(uid)
                handle = storage.handle_for(uid)
                root = public_root(handle) if handle else ""
                text = (
                    f"Persistent storage: this requester has /storage ({used / MB:.1f} of {quota // MB} MB used, "
                    f"{files} files). It survives sandbox wipes; /workspace does not. Save finished work, "
                    "builds and files they will want later in /storage (never caches, node_modules or venvs). "
                    "Anything in /storage/public/ is a public website"
                    + (f" at {root}" if root else " at their /u/<handle>/ address (storage status shows it)")
                    + ". Use the storage tool to list, move, share links or set the address, and "
                    "send_file path=/storage/... to attach a stored file."
                )
        except Exception:
            logger.debug("storage prompt status failed", exc_info=True)
            text = ""
        if len(cache) > 2000:
            cache.clear()
        cache[uid] = (time.monotonic(), text)
        return text

    ctx.register_prompt(
        PromptComponent(
            id="user_storage.status",
            plugin="user_storage",
            position="tools",
            requires_tools=("shell", "storage"),
            render=render,
        )
    )
    return [StorageTool(bot)]


async def teardown(bot):
    if getattr(bot, "_user_storage", None) is not None:
        bot._user_storage = None
