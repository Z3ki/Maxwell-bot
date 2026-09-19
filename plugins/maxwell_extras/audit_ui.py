"""Discord tool-call disclosure UI.

Records tools that actually executed for an inbound turn and attaches a small
button to Maxwell's final Discord reply. Clicking it opens an ephemeral embed
with the real tool names, sanitized arguments, result status, and whether a web
search was model-requested. This gives users a concrete way to
separate "Maxwell searched" from an unsourced/hallucinated answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from contextvars import ContextVar
from pathlib import Path
from types import MethodType
from typing import Any

import discord

from autofix import redact_diagnostics, sanitize_tool_args

_MAX_TRACE_RESPONSES = 500
_MAX_CALLS_PER_TURN = 40
_MAX_RESULT_PREVIEW = 700
_WEB_SOURCE: ContextVar[str] = ContextVar("maxwell_web_source", default="direct")
_MESSAGE_BOTS: dict[str, Any] = {}
_TOOL_STARTED: dict[tuple[str, str], float] = {}
_PROGRESS_PATCHED = False
# The reply itself is not a hidden tool. Recording send_message (and the
# other delivery tools) made every hi show "Tools · 1" and dumped the
# execution embed into personal-app chats.
_DELIVERY_TOOLS = {
    "send_message",
    "send_file",
    "send_rich_message",
    "send_media",
    "send_meme",
    "tts",
    "create_poll",
    "no_response",
}
_VISIBLE_RESULT_MARKERS = (
    "__MESSAGE_SENT__",
    "__FILE_SENT__",
    "__MEDIA_SENT__",
    "__MEME_SENT__",
    "__TTS_SENT__",
    "__POLL_SENT__",
)


def _mid(message: Any) -> str:
    return str(getattr(message, "id", "") or "")


def _trim_result(value: Any) -> str:
    text = redact_diagnostics(str(value or ""))
    # Do not persist binary payloads/large model attachments in audit history.
    for marker in ("__IMAGE_B64__", "__AUDIO_B64__"):
        if marker in text:
            text = text.split(marker, 1)[0].rstrip() + f" [{marker[2:-2].lower()} omitted]"
    text = " ".join(text.split())
    if len(text) > _MAX_RESULT_PREVIEW:
        text = text[:_MAX_RESULT_PREVIEW] + "…"
    return text


def _safe_params(params: Any) -> Any:
    try:
        return sanitize_tool_args(params if isinstance(params, dict) else {"value": params})
    except Exception:
        return {"value": "[unavailable]"}


def _is_delivery_tool(name: str, *, tool: Any = None, result: Any = "") -> bool:
    """True when the tool *is* the user-visible Discord reply."""
    if str(name or "") in _DELIVERY_TOOLS:
        return True
    if tool is not None and bool(getattr(tool, "produces_visible_output", False)):
        return True
    text = str(result or "")
    return any(marker in text for marker in _VISIBLE_RESULT_MARKERS)


class ToolAuditStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._live: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)

    async def record(self, message: Any, entry: dict) -> None:
        inbound = _mid(message)
        if not inbound:
            return
        async with self._lock:
            rows = self._live.setdefault(inbound, [])
            if len(rows) < _MAX_CALLS_PER_TURN:
                rows.append(entry)
            # The same message object is also what ToolProgress holds.
            _MESSAGE_BOTS[inbound] = getattr(message, "_maxwell_audit_bot", None) or _MESSAGE_BOTS.get(inbound)
            if len(_MESSAGE_BOTS) > 1000:
                for key in list(_MESSAGE_BOTS)[:250]:
                    _MESSAGE_BOTS.pop(key, None)

    async def has_live(self, inbound_id: str) -> bool:
        async with self._lock:
            return bool(self._live.get(str(inbound_id)))

    async def finalize(self, inbound_id: str, response_id: str) -> int:
        inbound_id, response_id = str(inbound_id), str(response_id)
        if not inbound_id or not response_id:
            return 0
        async with self._lock:
            calls = list(self._live.get(inbound_id) or [])
            if not calls:
                return 0
            data = self._read()
            data[response_id] = {
                "inbound_id": inbound_id,
                "created_at": time.time(),
                "calls": calls,
            }
            if len(data) > _MAX_TRACE_RESPONSES:
                ordered = sorted(
                    data.items(),
                    key=lambda item: float((item[1] or {}).get("created_at") or 0),
                    reverse=True,
                )[:_MAX_TRACE_RESPONSES]
                data = dict(ordered)
            self._write(data)
            # Keep it live for multi-chunk responses; finalizing again is harmless.
            return len(calls)

    async def get_response(self, response_id: str) -> dict | None:
        async with self._lock:
            row = self._read().get(str(response_id))
            return dict(row) if isinstance(row, dict) else None


class ToolTraceView(discord.ui.View):
    def __init__(self, bot: Any, count: int | None = None):
        super().__init__(timeout=None)
        label = f"Tools · {count}" if count else "Tools used"
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            emoji="🔎",
            custom_id="maxwell:tool-trace",
        )
        button.callback = self._show  # type: ignore[method-assign]
        self.add_item(button)
        self.bot = bot

    async def _show(self, interaction: discord.Interaction) -> None:
        store = getattr(self.bot, "_maxwell_tool_audit_store", None)
        response_id = str(getattr(getattr(interaction, "message", None), "id", "") or "")
        row = await store.get_response(response_id) if store is not None else None
        if not row:
            await interaction.response.send_message(
                "No retained tool trace is available for this reply.", ephemeral=True
            )
            return
        calls = list(row.get("calls") or [])
        embed = discord.Embed(
            title=f"Actual tool calls · {len(calls)}",
            description=(
                "This is execution data recorded by Maxwell, not a model-generated claim. "
                "If `web_search` is absent, this reply did not run that search tool."
            ),
            colour=discord.Colour.blurple(),
        )
        for index, call in enumerate(calls[:20], start=1):
            name = str(call.get("name") or "unknown")
            source = str(call.get("source") or "model")
            elapsed = call.get("elapsed_ms")
            status = "error" if call.get("error") else "ok"
            args = call.get("params") or {}
            try:
                args_text = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                args_text = str(args)
            if len(args_text) > 350:
                args_text = args_text[:350] + "…"
            result = str(call.get("result") or "")
            if len(result) > 550:
                result = result[:550] + "…"
            timing = f" · {elapsed}ms" if elapsed is not None else ""
            value = f"**{status}** · source `{source}`{timing}\n`{args_text or '{}'} `"
            if result:
                value += f"\n{result}"
            embed.add_field(name=f"{index}. {name}", value=value[:1024], inline=False)
        if len(calls) > 20:
            embed.set_footer(text=f"Showing first 20 of {len(calls)} calls")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _record_call(bot: Any, message: Any, *, name: str, params: Any, result: Any = "", error: bool = False, started: float, source: str = "model", tool: Any = None) -> None:
    if _is_delivery_tool(name, tool=tool, result=result):
        return
    store = getattr(bot, "_maxwell_tool_audit_store", None)
    if store is None:
        return
    inbound = _mid(message)
    if inbound:
        _MESSAGE_BOTS[inbound] = bot
    entry = {
        "name": str(name),
        "source": str(source),
        "params": _safe_params(params),
        "result": _trim_result(result),
        "error": bool(error),
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "at": time.time(),
    }
    await store.record(message, entry)


async def _attach_trace(bot: Any, sent: Any, inbound_message: Any) -> None:
    store = getattr(bot, "_maxwell_tool_audit_store", None)
    if store is None or sent is None:
        return
    inbound_id, response_id = _mid(inbound_message), _mid(sent)
    count = await store.finalize(inbound_id, response_id)
    if not count:
        return
    edit = getattr(sent, "edit", None)
    if callable(edit):
        with contextlib.suppress(Exception):
            await edit(view=ToolTraceView(bot, count=count))


def _patch_progress_transition() -> None:
    global _PROGRESS_PATCHED
    if _PROGRESS_PATCHED:
        return
    try:
        from tool_progress import ToolProgress
    except Exception:
        return
    original = ToolProgress.transition_to_final
    if getattr(original, "_maxwell_audit_wrapped", False):
        _PROGRESS_PATCHED = True
        return

    async def transition(self: Any, content: str) -> bool:
        ok = await original(self, content)
        if not ok:
            return ok
        inbound = getattr(self, "_msg", None)
        bot = _MESSAGE_BOTS.get(_mid(inbound))
        posted = getattr(self, "posted", None)
        if bot is not None and posted is not None:
            with contextlib.suppress(Exception):
                await _attach_trace(bot, posted, inbound)
        return ok

    transition._maxwell_audit_wrapped = True  # type: ignore[attr-defined]
    ToolProgress.transition_to_final = transition
    _PROGRESS_PATCHED = True


def install_tool_audit(bot: Any, ctx: Any) -> ToolAuditStore:
    """Install idempotent wrappers around real tool execution and final sends."""
    store = ToolAuditStore(ctx.store_path("tool_traces.json"))
    bot._maxwell_tool_audit_store = store

    if not getattr(bot, "_maxwell_tool_audit_view_registered", False):
        add_view = getattr(bot, "add_view", None)
        if callable(add_view):
            with contextlib.suppress(Exception):
                add_view(ToolTraceView(bot))
                bot._maxwell_tool_audit_view_registered = True

    if ctx is not None and hasattr(ctx, "register_hook") and not getattr(
        bot, "_maxwell_tool_audit_execute_wrapped", False
    ):
        async def before_tool(payload) -> None:
            data = getattr(payload, "data", payload) or {}
            inbound = _mid(data.get("message"))
            name = str(data.get("name") or "")
            if inbound and name:
                _TOOL_STARTED[(inbound, name)] = time.monotonic()

        async def after_tool(payload) -> None:
            data = getattr(payload, "data", payload) or {}
            inbound = _mid(data.get("message"))
            name = str(data.get("name") or "")
            started = _TOOL_STARTED.pop((inbound, name), time.monotonic())
            await _record_call(
                data.get("bot") or bot,
                data.get("message"),
                name=name,
                params=dict(data.get("params") or {}),
                result=str(data.get("result") or ""),
                error=str(data.get("result") or "").startswith(("Error:", "Error ")),
                started=started,
                tool=data.get("tool"),
            )

        ctx.register_hook("before_tool", before_tool, priority=50)
        ctx.register_hook("after_tool", after_tool, priority=50)
        bot._maxwell_tool_audit_execute_wrapped = True
    elif not getattr(bot, "_maxwell_tool_audit_execute_wrapped", False):
        original_execute = getattr(bot, "_execute_tool_by_name", None)
        if callable(original_execute):
            async def execute_wrapper(self_obj: Any, message: Any, name: str, params: dict, *, disabled: set, compatible: set) -> str:
                # Mark the inbound early so progress-message transition can find this bot.
                inbound = _mid(message)
                if inbound:
                    _MESSAGE_BOTS[inbound] = self_obj
                started = time.monotonic()
                if str(name) == "web_search":
                    token = _WEB_SOURCE.set("model")
                    try:
                        return await original_execute(message, name, params, disabled=disabled, compatible=compatible)
                    finally:
                        _WEB_SOURCE.reset(token)
                try:
                    result = await original_execute(message, name, params, disabled=disabled, compatible=compatible)
                except Exception as exc:
                    await _record_call(self_obj, message, name=name, params=params, result=f"{type(exc).__name__}: {exc}", error=True, started=started)
                    raise
                await _record_call(self_obj, message, name=name, params=params, result=result, error=str(result).startswith(("Error:", "Error ")), started=started)
                return result

            bot._execute_tool_by_name = MethodType(execute_wrapper, bot)
            bot._maxwell_tool_audit_execute_wrapped = True

    # Direct web_search.execute() bypasses the dispatcher (tests, plugins).
    # Wrap the tool instance so those searches are recorded too.
    web_tool = (getattr(bot, "tools", None) or {}).get("web_search")
    if web_tool is not None and not getattr(web_tool, "_maxwell_tool_audit_wrapped", False):
        original_web_execute = getattr(web_tool, "execute", None)
        if callable(original_web_execute):
            async def web_wrapper(self_tool: Any, message: Any, *args: Any, **kwargs: Any) -> str:
                started = time.monotonic()
                params = dict(kwargs)
                if args:
                    params["args"] = list(args)
                source = _WEB_SOURCE.get()
                try:
                    result = await original_web_execute(message, *args, **kwargs)
                except Exception as exc:
                    await _record_call(bot, message, name="web_search", params=params, result=f"{type(exc).__name__}: {exc}", error=True, started=started, source=source)
                    raise
                await _record_call(bot, message, name="web_search", params=params, result=result, error=str(result).startswith(("Error:", "Error ")), started=started, source=source)
                return result

            web_tool.execute = MethodType(web_wrapper, web_tool)
            web_tool._maxwell_tool_audit_wrapped = True

    if not getattr(bot, "_maxwell_tool_audit_send_wrapped", False):
        original_send = getattr(bot, "_send_with_slowmode", None)
        if callable(original_send):
            async def send_wrapper(self_obj: Any, channel: Any, content: str | None = None, *, reply_to: Any = None, file: Any = None, **kwargs: Any) -> Any:
                sent = await original_send(channel, content, reply_to=reply_to, file=file, **kwargs)
                if sent is not None and reply_to is not None:
                    await _attach_trace(self_obj, sent, reply_to)
                return sent

            bot._send_with_slowmode = MethodType(send_wrapper, bot)
            bot._maxwell_tool_audit_send_wrapped = True

    _patch_progress_transition()
    return store
