"""Discord user-install progress behavior for slow/tool-backed commands.

Fast commands keep Discord's deferred interaction as the answer. If a command
runs a tool or is still unanswered after ten seconds, the deferred interaction
is changed to a stable ``working on it…`` status and the eventual answer is
sent as a separate follow-up message.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from user_install import UserInstallSession, is_user_install_command, is_user_install_message

_SLOW_AFTER_SECONDS = 10.0
_STATUS_TEXT = "working on it…"
_STATE_TTL_SECONDS = 15 * 60.0
_STATES: dict[str, "_InteractionProgressState"] = {}
_SESSION_PATCHED = False
_TOOL_PROGRESS_PATCHED = False


def _key(interaction: Any) -> str:
    value = getattr(interaction, "id", None)
    return str(value if value is not None else id(interaction))


class _InteractionProgressState:
    def __init__(self, interaction: Any):
        self.interaction = interaction
        self.started = time.monotonic()
        self.escalated = False
        self.status_set = False
        self.completed = False
        self.session: Any = None
        self.timer: asyncio.Task | None = None
        self.cleanup: asyncio.Task | None = None
        self.lock = asyncio.Lock()


def _state_for_interaction(interaction: Any) -> _InteractionProgressState | None:
    return _STATES.get(_key(interaction))


def _state_for_message(message: Any) -> _InteractionProgressState | None:
    interaction = getattr(message, "interaction", None)
    return _state_for_interaction(interaction) if interaction is not None else None


def _spawn(coro: Any) -> asyncio.Task | None:
    try:
        return asyncio.create_task(coro)
    except RuntimeError:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None


def _begin(interaction: Any) -> _InteractionProgressState:
    key = _key(interaction)
    old = _STATES.get(key)
    if old is not None:
        return old
    state = _InteractionProgressState(interaction)
    _STATES[key] = state
    state.timer = _spawn(_slow_timer(state))
    state.cleanup = _spawn(_cleanup_later(key, state))
    return state


async def _cleanup_later(key: str, state: _InteractionProgressState) -> None:
    await asyncio.sleep(_STATE_TTL_SECONDS)
    if _STATES.get(key) is state:
        _STATES.pop(key, None)


def _cancel_timer(state: _InteractionProgressState) -> None:
    task = state.timer
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
    state.timer = None


async def _edit_original(
    interaction: Any,
    content: str | None,
    **payload: Any,
) -> Any | None:
    """Edit the deferred response without flattening rich Discord payloads."""

    edit_payload = dict(payload)
    edit_payload["content"] = content
    edit = getattr(interaction, "edit_original_response", None)
    if callable(edit):
        return await edit(**edit_payload)
    original = getattr(interaction, "original_response", None)
    if callable(original):
        message = await original()
        message_edit = getattr(message, "edit", None)
        if callable(message_edit):
            updated = await message_edit(**edit_payload)
            return updated or message
    return None


async def _mark_working(state: _InteractionProgressState) -> Any | None:
    async with state.lock:
        if state.completed:
            return None
        state.escalated = True
        _cancel_timer(state)
        if state.session is not None:
            # Count the original response so UserInstallSession's documented
            # 1-original + 5-follow-up cap remains true.
            state.session._sent = max(
                int(getattr(state.session, "_sent", 0) or 0), 1
            )
        if state.status_set:
            original = getattr(state.interaction, "original_response", None)
            if callable(original):
                with contextlib.suppress(Exception):
                    return await original()
            return None
        try:
            posted = await _edit_original(state.interaction, _STATUS_TEXT)
        except Exception:
            # A tool can race the initial defer. Keep ``escalated`` true so the
            # answer still becomes a follow-up; the next send retries the status.
            return None
        state.status_set = posted is not None
        return posted


async def _slow_timer(state: _InteractionProgressState) -> None:
    try:
        delay = max(0.0, _SLOW_AFTER_SECONDS - (time.monotonic() - state.started))
        await asyncio.sleep(delay)
        if not state.completed:
            await _mark_working(state)
    except asyncio.CancelledError:
        pass


def _patch_session() -> None:
    global _SESSION_PATCHED
    if _SESSION_PATCHED:
        return

    original_init = UserInstallSession.__init__
    next_send = {"fn": UserInstallSession._send_impl}

    def session_init(self: Any, interaction: Any) -> None:
        original_init(self, interaction)
        state = _state_for_interaction(interaction)
        self._maxwell_progress_state = state
        if state is not None:
            state.session = self
            if state.escalated:
                self._sent = max(int(getattr(self, "_sent", 0) or 0), 1)

    async def session_send(
        self: Any,
        content: str | None = None,
        file: Any = None,
        **kwargs: Any,
    ) -> Any:
        state = getattr(self, "_maxwell_progress_state", None)
        if state is None:
            return await next_send["fn"](self, content=content, file=file, **kwargs)

        await self.ensure_deferred()
        elapsed = time.monotonic() - state.started
        if not state.completed and (
            (not state.escalated and elapsed >= _SLOW_AFTER_SECONDS)
            or (state.escalated and not state.status_set)
        ):
            await _mark_working(state)

        # Keep the rich payload when the final fast answer edits Discord's
        # deferred original response. This matters for /maxwell, whose text is
        # intentionally converted to an embed by the outer transport wrapper.
        fast_payload: dict[str, Any] = {}
        if kwargs.get("embed") is not None:
            fast_payload["embed"] = kwargs["embed"]
        elif kwargs.get("embeds"):
            fast_payload["embeds"] = kwargs["embeds"]
        for key in ("view", "allowed_mentions", "suppress_embeds"):
            if key in kwargs and kwargs[key] is not None:
                fast_payload[key] = kwargs[key]

        extra_files = kwargs.get("files")
        has_file_payload = file is not None or bool(extra_files)
        has_followup_only_payload = kwargs.get("poll") is not None

        # A fast response should replace Discord's deferred interaction instead
        # of creating a second message. Use the same lock as the slow timer so
        # "working on it…" can never overwrite a final answer at the boundary.
        promote_after_fast_attempt = False
        if (
            not has_file_payload
            and not has_followup_only_payload
            and int(getattr(self, "_sent", 0) or 0) == 0
        ):
            async with state.lock:
                if not state.escalated and not state.completed:
                    # Re-check the cutoff while holding the lock. This closes
                    # the gap between the earlier elapsed check and this edit.
                    if time.monotonic() - state.started >= _SLOW_AFTER_SECONDS:
                        promote_after_fast_attempt = True
                    else:
                        text = None if content is None else str(content)
                        if text == "":
                            text = None
                        visible_content = text
                        if visible_content is None and not fast_payload:
                            visible_content = "\u200b"
                        try:
                            sent = await _edit_original(
                                self.interaction,
                                visible_content,
                                **fast_payload,
                            )
                        except Exception:
                            sent = None
                        if sent is not None:
                            state.completed = True
                            _cancel_timer(state)
                            self._sent = 1
                            self._last = sent
                            return sent
                        promote_after_fast_attempt = True

        # If the cutoff arrived while the fast path was being considered, or
        # Discord rejected the original-response edit, promote after releasing
        # the lock and send the answer as a follow-up.
        if promote_after_fast_attempt:
            await _mark_working(state)

        # Files/polls cannot use the lightweight original-response edit path.
        # Promote to status + follow-up instead.
        if (
            (has_file_payload or has_followup_only_payload)
            and not state.escalated
            and not state.completed
        ):
            await _mark_working(state)

        return await next_send["fn"](self, content=content, file=file, **kwargs)

    session_init._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
    session_send._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
    UserInstallSession.__init__ = session_init

    def progress_factory(original_send):
        next_send["fn"] = original_send
        return session_send

    from user_install import wrap_session_send

    wrap_session_send(progress_factory, name="interaction_progress", priority=50)
    _SESSION_PATCHED = True


def _patch_tool_progress() -> None:
    """Keep user-install progress on the original interaction message."""
    global _TOOL_PROGRESS_PATCHED
    if _TOOL_PROGRESS_PATCHED:
        return
    try:
        from tool_progress import ToolProgress
    except Exception:
        return
    if getattr(ToolProgress._post_reply, "_maxwell_interaction_progress_wrapped", False):
        _TOOL_PROGRESS_PATCHED = True
        return

    original_post = ToolProgress._post_reply
    original_stop = ToolProgress.stop

    async def post_reply(self: Any, content: str) -> Any:
        if str(getattr(self, "_platform", "")) == "user_install":
            state = _state_for_message(getattr(self, "_msg", None))
            if state is not None:
                posted = await _mark_working(state)
                if posted is not None:
                    return posted
        return await original_post(self, content)

    async def stop(self: Any) -> None:
        if str(getattr(self, "_platform", "")) != "user_install":
            await original_stop(self)
            return
        # The original interaction is a persistent status by design; do not
        # delete it when a tool batch finishes.
        self._stopped = True
        for attr in ("_post_task", "_deferred_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
            setattr(self, attr, None)
        self._posted = None

    post_reply._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
    stop._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
    ToolProgress._post_reply = post_reply
    ToolProgress.stop = stop
    _TOOL_PROGRESS_PATCHED = True


def install_interaction_progress(bot: Any, ctx: Any = None) -> None:
    """Install the 10-second/tool escalation behavior once for this bot."""
    if getattr(bot, "_maxwell_interaction_progress_installed", False):
        return

    _patch_session()
    _patch_tool_progress()

    async def pre_handler(_bot_obj: Any, interaction: Any) -> bool:
        if is_user_install_command(interaction):
            _begin(interaction)
        return False

    from user_install import register_interaction_handler

    register_interaction_handler(
        pre_handler, priority=20, name="interaction_progress"
    )

    async def before_tool(payload) -> None:
        data = getattr(payload, "data", payload) or {}
        message = data.get("message")
        if message is not None and is_user_install_message(message):
            state = _state_for_message(message)
            if state is not None:
                await _mark_working(state)

    if ctx is not None and hasattr(ctx, "register_hook"):
        ctx.register_hook("before_tool", before_tool, priority=40)
    elif hasattr(bot, "hooks") and bot.hooks is not None:
        bot.hooks.register("maxwell_extras", "before_tool", before_tool, priority=40)

    bot._maxwell_interaction_progress_installed = True


__all__ = ["install_interaction_progress"]
