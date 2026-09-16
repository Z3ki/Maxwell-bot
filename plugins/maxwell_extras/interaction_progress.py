"""Discord user-install progress behavior for slow/tool-backed commands.

Fast commands keep Discord's deferred interaction as the answer. If a command
runs a tool or is still unanswered after five seconds, the deferred interaction
is changed to a stable ``working on it…`` status and the eventual answer is
sent as a separate follow-up message.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import MethodType
from typing import Any

from user_install import UserInstallSession, is_user_install_command, is_user_install_message

_SLOW_AFTER_SECONDS = 5.0
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


async def _edit_original(interaction: Any, content: str) -> Any | None:
    edit = getattr(interaction, "edit_original_response", None)
    if callable(edit):
        return await edit(content=content)
    original = getattr(interaction, "original_response", None)
    if callable(original):
        message = await original()
        message_edit = getattr(message, "edit", None)
        if callable(message_edit):
            updated = await message_edit(content=content)
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
    if _SESSION_PATCHED or getattr(
        UserInstallSession.send, "_maxwell_interaction_progress_wrapped", False
    ):
        _SESSION_PATCHED = True
        return

    original_init = UserInstallSession.__init__
    original_send = UserInstallSession.send

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
            return await original_send(self, content=content, file=file, **kwargs)

        await self.ensure_deferred()
        elapsed = time.monotonic() - state.started
        if not state.completed and (
            (not state.escalated and elapsed >= _SLOW_AFTER_SECONDS)
            or (state.escalated and not state.status_set)
        ):
            await _mark_working(state)

        # A fast, text-only response should replace Discord's deferred
        # interaction instead of creating a second message.
        if (
            not state.escalated
            and not state.completed
            and file is None
            and int(getattr(self, "_sent", 0) or 0) == 0
        ):
            text = None if content is None else str(content)
            if text == "":
                text = None
            try:
                sent = await _edit_original(self.interaction, text or "\u200b")
            except Exception:
                sent = None
            if sent is not None:
                state.completed = True
                _cancel_timer(state)
                self._sent = 1
                self._last = sent
                return sent
            # If Discord won't let us edit the deferred original, make the
            # state explicit and fall back to a normal follow-up.
            await _mark_working(state)

        # Files cannot be attached with the lightweight original-response
        # edit path used here. Promote to status + follow-up instead.
        if file is not None and not state.escalated and not state.completed:
            await _mark_working(state)

        return await original_send(self, content=content, file=file, **kwargs)

    session_init._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
    session_send._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
    UserInstallSession.__init__ = session_init
    UserInstallSession.send = session_send
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


def install_interaction_progress(bot: Any) -> None:
    """Install the 5-second/tool escalation behavior once for this bot."""
    if getattr(bot, "_maxwell_interaction_progress_installed", False):
        return

    _patch_session()
    _patch_tool_progress()

    # bot.py imported handle_user_install_interaction directly, so patch the
    # global used by on_interaction rather than importing bot.py a second time
    # (which would happen when bot.py is running as __main__).
    on_interaction = getattr(bot, "on_interaction", None)
    func = getattr(on_interaction, "__func__", on_interaction)
    namespace = getattr(func, "__globals__", None)
    if isinstance(namespace, dict):
        original_handler = namespace.get("handle_user_install_interaction")
        if callable(original_handler) and not getattr(
            original_handler, "_maxwell_interaction_progress_wrapped", False
        ):
            async def handler_wrapper(bot_obj: Any, interaction: Any) -> bool:
                if is_user_install_command(interaction):
                    _begin(interaction)
                return await original_handler(bot_obj, interaction)

            handler_wrapper._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
            namespace["handle_user_install_interaction"] = handler_wrapper

    original_execute = getattr(bot, "_execute_tool_by_name", None)
    if callable(original_execute) and not getattr(
        original_execute, "_maxwell_interaction_progress_wrapped", False
    ):
        async def execute_wrapper(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
            message = args[0] if args else kwargs.get("message")
            if message is not None and is_user_install_message(message):
                state = _state_for_message(message)
                if state is not None:
                    await _mark_working(state)
            return await original_execute(*args, **kwargs)

        execute_wrapper._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
        bot._execute_tool_by_name = MethodType(execute_wrapper, bot)

    # Automatic freshness web searches bypass _execute_tool_by_name.
    web_tool = (getattr(bot, "tools", None) or {}).get("web_search")
    web_execute = getattr(web_tool, "execute", None) if web_tool is not None else None
    if callable(web_execute) and not getattr(
        web_execute, "_maxwell_interaction_progress_wrapped", False
    ):
        async def web_wrapper(self_tool: Any, message: Any, *args: Any, **kwargs: Any) -> Any:
            if is_user_install_message(message):
                state = _state_for_message(message)
                if state is not None:
                    await _mark_working(state)
            return await web_execute(message, *args, **kwargs)

        web_wrapper._maxwell_interaction_progress_wrapped = True  # type: ignore[attr-defined]
        web_tool.execute = MethodType(web_wrapper, web_tool)

    bot._maxwell_interaction_progress_installed = True


__all__ = ["install_interaction_progress"]
