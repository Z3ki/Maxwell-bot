"""Routing guardrails for unattended autonomy actions.

The planner may observe more rooms than it is allowed to speak in. This module
keeps those two concepts separate: activity can still be useful context, but a
guild post/tool action must resolve to a configured autonomy channel. It also
prevents a numbered reply target from being sent in a different room and
removes fallback guessing for visible channel-posting tools.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

import discord

from autonomy import AUTONOMY_POST_TOOLS, AutonomyEngine

_INSTALLED = False


def _digits(value: Any) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _configured_channels(engine: Any) -> set[str]:
    out: set[str] = set()
    bot = getattr(engine, "bot", None)
    for raw in getattr(bot, "_auto_channels", None) or set():
        cid = _digits(raw)
        if cid:
            out.add(cid)
    return out


def _known_kind(engine: Any, cid: str) -> str:
    idx = getattr(engine, "_context_index", None)
    kind_by_id = getattr(idx, "kind_by_id", None)
    if isinstance(kind_by_id, dict):
        kind = str(kind_by_id.get(cid) or "")
        if kind:
            return kind

    bot = getattr(engine, "bot", None)
    channel = None
    getter = getattr(bot, "get_channel", None)
    if callable(getter) and cid.isdigit():
        with contextlib.suppress(Exception):
            channel = getter(int(cid))
    if channel is None:
        return "unknown"
    if isinstance(channel, discord.DMChannel):
        return "dm"
    if isinstance(channel, discord.GroupChannel):
        return "group"
    if getattr(channel, "guild", None) is not None:
        return "guild"
    return "unknown"


def _route_allowed(engine: Any, raw_cid: Any) -> tuple[bool, str, str]:
    """Return allowed, normalized channel id, human-readable denial reason."""

    cid = _digits(raw_cid)
    if not cid:
        return False, "", "missing target_channel_id; autonomy will not guess a room"

    channel_allowed = getattr(engine, "_channel_allowed", None)
    if callable(channel_allowed):
        try:
            if not channel_allowed(cid):
                return False, cid, "channel is blocked by Maxwell/autonomy controls"
        except Exception:
            return False, cid, "could not verify channel policy safely"

    kind = _known_kind(engine, cid)
    if kind == "dm":
        return False, cid, "DM channels must use send_dm instead of post_channel"
    if kind == "group":
        idx = getattr(engine, "_context_index", None)
        handle_by_id = getattr(idx, "handle_by_id", None)
        if isinstance(handle_by_id, dict) and cid in handle_by_id:
            return True, cid, ""
        return False, cid, "group DM was not part of this tick's addressable context"

    configured = _configured_channels(engine)
    if cid not in configured:
        return (
            False,
            cid,
            "guild channel is context-only; autonomous speech is limited to configured auto channels",
        )
    return True, cid, ""


def _reply_channel(engine: Any, reply_message_id: Any) -> str | None:
    mid = _digits(reply_message_id)
    if not mid:
        return None
    idx = getattr(engine, "_context_index", None)
    reverse = getattr(idx, "msg_idx_by_id", None)
    by_idx = getattr(idx, "message_channel_by_idx", None)
    if not isinstance(reverse, dict) or not isinstance(by_idx, dict):
        return None
    msg_idx = reverse.get(mid)
    if msg_idx is None:
        return None
    cid = _digits(by_idx.get(msg_idx))
    return cid or None


def _reply_matches_route(
    engine: Any, action: dict[str, Any], cid: str
) -> tuple[bool, str]:
    reply_id = action.get("reply_to_message_id")
    if not reply_id:
        return True, ""
    reply_cid = _reply_channel(engine, reply_id)
    if reply_cid and reply_cid != cid:
        return (
            False,
            f"reply target belongs to channel {reply_cid}, not target channel {cid}",
        )
    return True, ""


def install_autonomy_routing_guards(bot: Any) -> None:
    """Patch AutonomyEngine with deterministic, fail-closed channel routing."""

    del bot
    global _INSTALLED
    if _INSTALLED or getattr(AutonomyEngine, "_maxwell_routing_guards", False):
        _INSTALLED = True
        return

    original_gate = AutonomyEngine.policy_gate
    original_post = AutonomyEngine._exec_post_channel
    original_tool = AutonomyEngine._exec_run_tool

    def stable_candidates(self: Any) -> list[str]:
        # Numeric sorting avoids lexical surprises such as 100 before 20 and
        # filters blocked channels before they ever become planner targets.
        candidates = {
            _digits(x) for x in (getattr(self.bot, "_auto_channels", None) or set())
        }
        candidates.discard("")
        allowed = []
        for cid in sorted(candidates, key=lambda value: int(value)):
            checker = getattr(self, "_channel_allowed", None)
            try:
                if callable(checker) and not checker(cid):
                    continue
            except Exception:
                continue
            allowed.append(cid)
        return allowed

    async def guarded_gate(self: Any, actions: list[dict]) -> list[Any]:
        verdicts = await original_gate(self, actions)
        for verdict in verdicts:
            if not getattr(verdict, "allowed", False):
                continue
            action = getattr(verdict, "action", None) or {}
            kind = str(action.get("kind") or "")
            tool_name = str(action.get("tool_name") or "")
            needs_channel_route = kind == "post_channel" or (
                kind == "run_tool" and tool_name in AUTONOMY_POST_TOOLS
            )
            if not needs_channel_route:
                continue
            ok, cid, reason = _route_allowed(self, action.get("target_channel_id"))
            if not ok:
                verdict.allowed = False
                verdict.code = "wrong_channel"
                verdict.reason = reason
                verdict.target_channel_id = cid or None
                continue
            reply_ok, reply_reason = _reply_matches_route(self, action, cid)
            if not reply_ok:
                verdict.allowed = False
                verdict.code = "wrong_reply_channel"
                verdict.reason = reply_reason
                verdict.target_channel_id = cid
        return verdicts

    async def guarded_post(self: Any, action: dict, result: dict) -> None:
        ok, cid, reason = _route_allowed(self, action.get("target_channel_id"))
        if not ok:
            result["result"] = "error"
            result["error"] = reason
            return
        reply_ok, reply_reason = _reply_matches_route(self, action, cid)
        if not reply_ok:
            result["result"] = "error"
            result["error"] = reply_reason
            return
        safe_action = dict(action)
        safe_action["target_channel_id"] = cid
        await original_post(self, safe_action, result)

    async def guarded_tool(self: Any, action: dict, result: dict) -> None:
        tool_name = str(action.get("tool_name") or "")
        if tool_name in AUTONOMY_POST_TOOLS:
            ok, cid, reason = _route_allowed(self, action.get("target_channel_id"))
            if not ok:
                result["result"] = "error"
                result["error"] = reason
                return
            safe_action = dict(action)
            safe_action["target_channel_id"] = cid
            await original_tool(self, safe_action, result)
            return
        await original_tool(self, action, result)

    stable_candidates._maxwell_routing_guards = True  # type: ignore[attr-defined]
    guarded_gate._maxwell_routing_guards = True  # type: ignore[attr-defined]
    guarded_post._maxwell_routing_guards = True  # type: ignore[attr-defined]
    guarded_tool._maxwell_routing_guards = True  # type: ignore[attr-defined]

    AutonomyEngine._auto_channel_candidates = stable_candidates
    AutonomyEngine.policy_gate = guarded_gate
    AutonomyEngine._exec_post_channel = guarded_post
    AutonomyEngine._exec_run_tool = guarded_tool
    AutonomyEngine._maxwell_routing_guards = True  # type: ignore[attr-defined]
    _INSTALLED = True


__all__ = ["install_autonomy_routing_guards"]
