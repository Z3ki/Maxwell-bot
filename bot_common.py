"""Shared module-level state for the Maxwell bot.

Split out of bot.py: imports, logging setup, ContextVars,
module constants and small helper functions live here so both
bot.py and the bot_parts mixins see the same globals.
"""


import asyncio
import base64
import contextlib
from contextvars import ContextVar
import hashlib
import html
import inspect
import io
import json
import logging
import math
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
from discord.ext import commands
from discord.utils import MISSING
from process_utils import communicate_process
from media_payloads import (
    merge_followup_media,
    sanitize_media_memory,
    strip_media_payloads,
)
from discord_media import (
    clean_media_url,
    discord_attachment_key,
    discord_message_link,
    media_urls_in_text,
    refresh_discord_media_url,
    resolve_discord_message,
)
from message_quota import (
    FREE_MESSAGE_LIMIT as FREE_MESSAGE_LIMIT,
    FREE_WINDOW_SECONDS as FREE_WINDOW_SECONDS,
    MessageQuota,
    MessageQuotaExceeded,
    enforced_message_limit,
    enforced_window_seconds,
)
from usage_commands import install_usage_commands

import watch_policy  # noqa: E402
import channel_watch  # noqa: E402
from concurrency_safety import (  # noqa: E402
    ChannelWorkQueues,
    KeyedLocks,
    cancel_once,
    loop_watchdog,
)
from message_pipeline import (  # noqa: E402
    InboundDedup,
    ReplyQueue,
    RequestJournal,
    Watermarks,
)
from generated_artifacts import begin_generated_files, reset_generated_files  # noqa: E402
from tooling.helpers import (  # noqa: E402
    ReasoningLogTool,
    notify_owner,
    _IMAGE_FETCH_UA,
    _ALL_MOD_TOOLS,
    DM_BLOCKED_TOOLS,
    _mod_tools_allowed,
    _GATED_MOD_TOOLS,
    _mod_tool_refusal,
    _is_youtube_url,
    preserve_message_delivery,
    _get_shared_session,
    _is_private_chat,
    _is_safe_url,
    _read_response_limited,
    close_shared_session,
    SITE_READ_LOOP_MARKER,
)
from maxwell_core.transport.context_helpers import (
    _fill_identity_text as _fill_identity_text,
    _format_context_timestamp as _format_context_timestamp,
    _live_account_name as _live_account_name,
    _live_self_identity_line as _live_self_identity_line,
    _live_self_member as _live_self_member,
    _live_self_name as _live_self_name,
    _memory_requester_for as _memory_requester_for,
    _web_result_snippet as _web_result_snippet,
)
from maxwell_core.prompts.conversation import (
    ConversationPromptBuilder, ConversationPromptHooks,
)
from maxwell_core.prompts.protocols import (
    DISCORD_CHAT_PROTOCOL as DISCORD_CHAT_PROTOCOL,
    LEAN_TOOL_PROTOCOL as LEAN_TOOL_PROTOCOL,
    MAXWELL_BASE_KNOWLEDGE as MAXWELL_BASE_KNOWLEDGE,
    TOOL_PROTOCOL as TOOL_PROTOCOL,
)

# Compatibility imports preserve bot.* for existing callers.
from maxwell_core.transport.output_safety import (
    ARTIFACT_BLOCK_RE as ARTIFACT_BLOCK_RE,
    DOUBLE_WRAPPED_URL_RE as DOUBLE_WRAPPED_URL_RE,
    GENERIC_PIPE_TOOL_RE as GENERIC_PIPE_TOOL_RE,
    KNOWN_TOOL_NAMES as KNOWN_TOOL_NAMES,
    LEAKED_CONTEXT_MARKER_RE as LEAKED_CONTEXT_MARKER_RE,
    LEAKED_MESSAGE_TAG_RE as LEAKED_MESSAGE_TAG_RE,
    LEAKED_TOOL_CALL_RE as LEAKED_TOOL_CALL_RE,
    PIPE_MARKER_RE as PIPE_MARKER_RE,
    PIPE_TOOL_CALL_RE as PIPE_TOOL_CALL_RE,
    PIPE_TOOL_RE as PIPE_TOOL_RE,
    TOKEN_ARTIFACT_RE as TOKEN_ARTIFACT_RE,
    TOOL_TRACE_LINE_RE as TOOL_TRACE_LINE_RE,
    TRANSCRIPT_MENTION_RE as TRANSCRIPT_MENTION_RE,
    UNTERMINATED_TOOL_STOP_RE as UNTERMINATED_TOOL_STOP_RE,
    WRAPPED_URL_RE as WRAPPED_URL_RE,
    _ARG_PAIR_RE as _ARG_PAIR_RE,
    _DSML_CONTENT_PARAM_RE as _DSML_CONTENT_PARAM_RE,
    _DSML_INVOKE_BLOCK_RE as _DSML_INVOKE_BLOCK_RE,
    _DSML_INVOKE_NAME_RE as _DSML_INVOKE_NAME_RE,
    _DSML_PARAMETER_BLOCK_RE as _DSML_PARAMETER_BLOCK_RE,
    _DSML_TAG_RE as _DSML_TAG_RE,
    _DSML_WRAPPED_INVOKE_RE as _DSML_WRAPPED_INVOKE_RE,
    _DSML_WRAPPED_PARAMETER_RE as _DSML_WRAPPED_PARAMETER_RE,
    _auto_format_discord as _auto_format_discord,
    _fenced_code_ranges as _fenced_code_ranges,
    _find_tool_close as _find_tool_close,
    _find_xml_tag_end as _find_xml_tag_end,
    _in_ranges as _in_ranges,
    _iter_top_level_tool_tags as _iter_top_level_tool_tags,
    _parse_xml_open_tag as _parse_xml_open_tag,
    _replace_dsml_invoke as _replace_dsml_invoke,
    _sanitize_visible_reply as _sanitize_visible_reply,
    _strip_arg_protocol_leaks as _strip_arg_protocol_leaks,
    _strip_dsml_tool_leaks as _strip_dsml_tool_leaks,
    _strip_leading_reasoning_json as _strip_leading_reasoning_json,
    _unwrap_openai_text_part as _unwrap_openai_text_part,
    extract_json_object as extract_json_object,
    strip_model_artifact_leaks as strip_model_artifact_leaks,
    strip_tool_payload_leaks as strip_tool_payload_leaks,
)
from maxwell_core.transport.attachments import (
    TEXT_ATTACHMENT_EXTS as TEXT_ATTACHMENT_EXTS,
    TEXT_ATTACHMENT_MAX_CHARS as TEXT_ATTACHMENT_MAX_CHARS,
    TEXT_MIME_TYPES as TEXT_MIME_TYPES,
    _decode_readable_text as _decode_readable_text,
    _decoded_looks_readable as _decoded_looks_readable,
    _is_text_attachment as _is_text_attachment,
    _looks_like_text as _looks_like_text,
)
from maxwell_core.tools.results import (
    FOLLOWUP_TOOL_NAMES as FOLLOWUP_TOOL_NAMES,
    _PROMISE_MAX_CHARS as _PROMISE_MAX_CHARS,
    _PROMISE_RE as _PROMISE_RE,
    _PROMISE_VERB as _PROMISE_VERB,
    _SHORT_FOLLOWUP_AFTER_SEND_CHARS as _SHORT_FOLLOWUP_AFTER_SEND_CHARS,
    _VISIBLE_RESULT_MARKERS as _VISIBLE_RESULT_MARKERS,
    _apply_send_followup_guard as _apply_send_followup_guard,
    _is_short_plaintext_followup as _is_short_plaintext_followup,
    _only_promise_results as _only_promise_results,
    _plugin_result_needs_followup as _plugin_result_needs_followup,
    _promises_followup_work as _promises_followup_work,
    _should_skip_plaintext_after_send as _should_skip_plaintext_after_send,
    _tool_results_need_followup as _tool_results_need_followup,
    _turn_sent_message as _turn_sent_message,
)
from maxwell_core.tools.dispatch import (
    ToolCircuitBreaker as ToolCircuitBreaker,
    _prepare_tool_params as _prepare_tool_params,
    is_permission_denied_result,
)

from config import Config  # noqa: E402
from identity import (  # noqa: E402
    configured_admin_ids,
    fill_identity,
    identity_values,
    parse_birthday,
    process_name,
)
from context_budget import (  # noqa: E402
    BudgetPlan,
    allocate,
)
from control_defaults import (  # noqa: E402
    DEAD_CONTROL_KEYS,
    DEFAULT_CONTROL,
    audio_input_enabled,
    guild_capability_for_tool,
    parse_bool,
    parse_int,
)
from inbox import (  # noqa: E402
    InboxStore,
    apply_inbox_action,
    needs_decision as inbox_needs_decision,
)
from web_references import (  # noqa: E402
    WEB_REFERENCE_INSTRUCTION as WEB_REFERENCE_INSTRUCTION,
    begin_web_references,
    ensure_web_references,
    reset_web_references,
)
from providers import (  # noqa: E402
    MIME_MAP,
    ProviderEmptyResponseError,
    ProviderUsageExhaustedError,
)
from conversation_memory import ConversationMemoryManager  # noqa: E402
from maxwell_core.memory.scope import MemoryRequester  # noqa: E402
from discord_threads import (  # noqa: E402
    ThreadStore,
    is_discord_thread,
)
from discord_account import (  # noqa: E402
    account_ids as _discord_account_ids,
    bot_intents,
    configured_bot_token,
    ensure_user_install_context,
    sync_application_commands,
)
from user_install import (  # noqa: E402
    USER_INSTALL_COMMANDS,
    handle_user_install_interaction,
    is_user_install_message,
)
from tool_progress import (  # noqa: E402
    is_hidden_progress_tool,
    make_progress as _make_tool_progress,
)
from tool_registry import (  # noqa: E402 — reasoning now rides inside tool calls
    extract_reasoning,
    record_reasoning,
)
from typing_status import (  # noqa: E402
    typing_acquire,
    typing_release,
    typing_resume,
    typing_suspend,
)
import site_backend  # noqa: E402
import site_server  # noqa: E402
import site_test  # noqa: E402
from plugin_manager import PluginManager  # noqa: E402
from tool_schemas import (  # noqa: E402
    REPLY_BURST_TOOL_NAMES,
    RESULT_TOOL_NAMES,
    TURN_ENDING_TOOL_NAMES,
    returns_result,
    build_openai_tools,
    contract_groups,
    elide_tool_calls_for_history,
    message_chars,
    normalize_native_tool_calls,
    recover_text_tool_calls,
    result_contract,
    trim_tool_tail,
)
from utils import (  # fd-safe, single source of truth  # noqa: E402
    FileLock,
    _atomic_json_write_sync,
    _coerce_utc_datetime,
    _compact_attachments_note,
    _compact_embeds_note,
    _safe_int,
    _spawn_background,
    format_reactions_annotation,
    is_gif_page_url,
    iter_message_payloads,
    iter_message_snapshots,
    message_combined_content,
    message_has_visible_payload,
    message_is_discord_system_event,
    message_reference_is_forward,
    render_discord_context_text,
)
from context_hydration import (  # noqa: E402
    hydrate_turn_context,
    hydration_value,
    is_pointer_only,
    message_text_without_mentions,
    reference_payload_usable,
)
from runtime_lifecycle import (  # noqa: E402
    begin_inflight,
    drain_inflight,
    drain_timeout_seconds,
    end_inflight,
    shard_launch_kwargs,
)


logger = logging.getLogger("maxwell")


class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


_log_format = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(_log_format)
_stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(_log_format)
_stderr_handler.setLevel(logging.ERROR)

# LOG_LEVEL has been in .env (and config.py) since forever but nothing ever
# read it — the root level was hardcoded to INFO, so `LOG_LEVEL=debug` did
# nothing and there was no way to see runtime values without editing code.
_LOG_LEVEL = getattr(
    logging, os.getenv("LOG_LEVEL", "info").strip().upper(), logging.INFO
)
logging.basicConfig(level=_LOG_LEVEL, handlers=[_stdout_handler, _stderr_handler])

# Third-party DEBUG is unusable noise: discord.gateway logs every heartbeat and
# aiohttp/httpx log every socket. Pin them a notch above ours so LOG_LEVEL=debug
# shows OUR values, not the websocket's.
if _LOG_LEVEL <= logging.DEBUG:
    for _noisy in (
        "discord",
        "discord.gateway",
        "discord.client",
        "discord.http",
        "aiohttp",
        "httpx",
        "httpcore",
        "urllib3",
        "asyncio",
        "PIL",
    ):
        logging.getLogger(_noisy).setLevel(logging.INFO)


_current_inbound: ContextVar[Any] = ContextVar("current_inbound", default=None)
_current_inbox_notices: ContextVar[tuple[str, ...]] = ContextVar("current_inbox_notices", default=())
_current_request_provider: ContextVar[Any] = ContextVar(
    "current_request_provider", default=None
)
_current_byok_tool_budget: ContextVar[Any] = ContextVar(
    "current_byok_tool_budget", default=None
)
# Personal-key turns get the same step budget as subagents. Eight was cute
# until a site edit burned all eight and then couldn't even say so.
BYOK_TOOL_CALL_LIMIT = 24
# Replying is the point of the turn, so it never spends the budget. Without
# this, a turn that ran out got its own send_message refused and went silent.
BYOK_BUDGET_EXEMPT_TOOLS = frozenset(
    {"send_message", "send_rich_message", "react", "no_response"}
)
_current_inbound_effects: ContextVar[Any] = ContextVar(
    "current_inbound_effects", default=None
)
_REQUEST_TERMINAL = frozenset({"delivered", "suppressed", "failed", "superseded"})

# Retained briefly so stale pre-migration confirmation state can expire.
_CONFIRM_TTL_SECONDS = 120.0

# Ceiling on remembered per-room watch state. Eviction costs a room one
# default-length watch window and nothing else.
_MAX_WATCH_STATES = 300
# Message ids remembered as "read untrusted content this turn". One turn's
# worth is all that is ever consulted; the cap only stops the dict growing
# for the life of the process.
_MAX_TAINTED_MESSAGES = 512

# Every one of these is keyed by channel or user and written on the hot path.
# None of them had an eviction rule, so each was a slow leak proportional to
# how many distinct rooms the bot had ever seen — which is exactly the number
# that grows when he is in a lot of servers.
_PER_CHANNEL_STATE_CAPS = {
    "_cooldowns": 2000,
    "_last_bot_reply": 1000,
    "_last_bot_send": 1000,
    "_recent_users": 500,
    "_typing_users": 500,
    "_active_request_user": 500,
    "_current_progress_by_channel": 500,
    "_media_context": 500,
    "_message_snapshots": 1000,
    "_message_update_state": 1000,
    # One 64-char digest per emoji-discussing channel; without a cap it grew
    # for the process lifetime in every guild the bot had ever seen.
    "_emoji_grid_shown": 500,
}

MAX_VISUAL_MEMORY_IMAGES = 5
# Keep visual carryover short. Long-lived image payloads make the model randomly
# talk about old screenshots in unrelated replies. That bug is creepy as hell.
MEDIA_CONTEXT_USES = 2
# Discord may deliver MESSAGE_CREATE before the embed unfurl. Give a directly
# addressed media turn a short chance to observe that preview before the first
# provider request; the edit handler remains the durable fallback for previews
# that arrive later.
LATE_EMBED_WAIT_SECONDS = 0.6
VISUAL_REFERENCE_RE = re.compile(
    r"(?i)\b("
    r"image|img|picture|pic|photo|screenshot|screen ?shot|attachment|media|"
    r"gif|meme|frame|video|clip|thumbnail|look at|see (?:it|this|that)|"
    r"what(?:'s| is) (?:in|on) (?:it|this|that)|describe (?:it|this|that)"
    r")\b"
)
PRIOR_VISUAL_REFERENCE_RE = re.compile(
    r"(?i)\b(previous|prior|earlier|last|old|recent|before|compare|again)\b"
)


# Secrets that must never reach a channel even inside an error string. Provider
# errors love to echo the request back, which historically meant the whole
# Authorization header.
_SECRET_IN_ERROR_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|(?:api[_-]?key|token|password|secret)\s*[=:]\s*\S+)"
)


def _tool_denial_text(name: str, exc: BaseException) -> str:
    """User-facing text for a denied tool call.

    Authorization-gate refusals start with ``refused:`` and carry safe,
    actionable causes (bad argument shape, disabled tool, budgets), so the
    model can correct itself. Any other exception text is masked.
    """
    reason = str(exc) if isinstance(exc, PermissionError) else ""
    if reason.startswith("refused:"):
        return f"Error: {reason}"
    text = f"Error - permission denied ({name})"
    if isinstance(exc, discord.Forbidden):
        text += "; check Maxwell's Discord permissions in this channel."
    return text


def _format_user_error(exc: BaseException, limit: int = 300) -> str:
    """One redacted line describing `exc`, safe to post in a channel.

    Operators could previously only tell a 429 from a crash by tailing pm2, so
    every report was "it just said sorry". This gives the channel the exception
    type plus a trimmed, secret-scrubbed message.
    """
    detail = " ".join(str(exc).split())
    detail = _SECRET_IN_ERROR_RE.sub("[redacted]", detail)
    if len(detail) > limit:
        detail = detail[: limit - 1].rstrip() + "…"
    name = type(exc).__name__
    return f"`{name}: {detail}`" if detail else f"`{name}`"


def _owner_audio_input_enabled(owner) -> bool:
    """Whether audio should be extracted and forwarded to the model."""
    return audio_input_enabled(owner)


def _message_created_at_iso(message) -> str:
    dt = _coerce_utc_datetime(getattr(message, "created_at", None))
    return (dt or datetime.now(timezone.utc)).isoformat()


async def _await_task_done(task: asyncio.Task) -> None:
    """Wait for an asyncio.Task to finish (whether cancelled, failed, or succeeded).

    Used by on_message's same-user interrupt so we know the prior task has
    fully released the channel lock before we try to acquire it.
    """
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # A cancelled predecessor is complete; a cancelled waiter must stop.
        # Shielding prevents cancellation from interrupting prior cleanup.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception as e:
        # By design: we only care that the task FINISHED (so it released the
        # channel lock), never why. The prior task's own handler already
        # reported any real failure.
        logger.debug("Awaited prior task finished with %s", type(e).__name__)


CUSTOM_EMOJI_ALIAS_RE = re.compile(r"(?<!<)(?<!<a):([A-Za-z0-9_]{2,32}):(?!\d)")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
TOOL_LINE_RE = re.compile(r"(?im)^\s*(?:TOOL|CALL)\s+([A-Za-z_]\w*)\s*[:\-]?\s*")
TEXT_ATTACHMENT_MAX_BYTES = 512 * 1024


def render_custom_emoji_aliases(
    text: str, emojis: dict[str, str], *, emoji_codes: dict[str, str] | None = None
) -> str:
    if not text or not emojis:
        return text

    # Fix broken AI-generated Discord emojis like <:blow_me:> or <a:catjam:>
    text = re.sub(r"<a?:([A-Za-z0-9_]{2,32}):>", r":\1:", text)
    # Prefer a live ID so an app emoji sharing a guild emoji's name stays the
    # selected asset. Fall back to the alias only when the ID is stale.
    from discord_expressions import custom_emoji_id
    codes = emoji_codes if emoji_codes is not None else {
        eid: code for code in emojis.values() if (eid := custom_emoji_id(code))
    }
    text = re.sub(
        r"<a?:([A-Za-z0-9_]{2,32}):(\d+)>",
        lambda m: codes.get(m.group(2)) or emojis.get(m.group(1).lower()) or m.group(0),
        text,
    )

    def replace(match: re.Match) -> str:
        return emojis.get(match.group(1).lower()) or match.group(0)

    return CUSTOM_EMOJI_ALIAS_RE.sub(replace, text)


# Discord error codes that all mean "the message you referenced is gone".
# 50035 arrives as a *400* ("Invalid Form Body / In message_reference: Unknown
# message") when the parent of a reply was deleted between our read and our
# send — NOT as a 404 — so catching discord.NotFound alone never handled it.
# 10008 (Unknown Message) is the 404 flavour of the same situation.
_UNKNOWN_REFERENCE_CODES = {50035, 10008}


def _is_unknown_reference_error(exc: Exception) -> bool:
    """True when a reply failed because its parent message no longer exists."""
    if isinstance(exc, discord.NotFound):
        return True
    code = getattr(exc, "code", None)
    if code in _UNKNOWN_REFERENCE_CODES:
        # 50035 is the generic "Invalid Form Body" code — it also covers bad
        # embeds, over-length content, and other payload errors we must NOT
        # swallow. Only the message_reference variant is recoverable by
        # re-sending without the reference.
        if code == 50035:
            return "message_reference" in str(exc).lower()
        return True
    return False


# Public hosted restrictions are opt-in on self-hosted installs. The shell
# uses its tenant-scoped gVisor backend in either mode.
OPERATOR_MAILBOX_TOOLS = frozenset({
    "inbox_list", "inbox_act", "email_send", "email_read_inbox", "email_get_message", "email_search",
})
_GLOBAL_IDENTITY_TOOLS = frozenset({
    "change_presence", "set_activity", "change_avatar", "set_nickname",
})
_CHESS_FOLLOWUP_TOOLS = frozenset({"chess_move", "chess_state", "chess_resign"})
_CHECKERS_FOLLOWUP_TOOLS = frozenset({
    "checkers_move", "checkers_state", "checkers_resign",
})


def _inactive_game_tools(message) -> set[str]:
    """Move, state, and resign stay hidden until that channel has a live game."""
    hidden = set(_CHESS_FOLLOWUP_TOOLS | _CHECKERS_FOLLOWUP_TOOLS)
    channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
    if not channel_id:
        return hidden
    try:
        from chess_game import get_manager

        game = get_manager().active(channel_id)
        if game is not None and not getattr(game, "is_over", True):
            hidden.difference_update(_CHESS_FOLLOWUP_TOOLS)
    except Exception:
        logger.debug("chess activity check failed", exc_info=True)
    try:
        from plugins.checkers.tools import get_game

        if get_game(channel_id) is not None:
            hidden.difference_update(_CHECKERS_FOLLOWUP_TOOLS)
    except Exception:
        logger.debug("checkers activity check failed", exc_info=True)
    return hidden

PUBLIC_RUNTIME_BLOCKED_TOOLS = frozenset({
    "agent_life", "user_sandbox", "spawn_background", "github_repo",
    "plugin_workbench", "manage_plugin", "update_base_personality",
    "update_server_prompt",
})

def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _str_set(data):
    return {str(x) for x in data}


_RETIRED_PREFIX_COMMANDS = frozenset(
    {
        "stop", "bg", "jobs", "job", "prompt", "clearprompt", "clearmem",
        "downvote", "neg", "summarize", "context", "rem",
        "drug", "sleep", "wake", "jailbreak", "progress", "ticket",
        "admin", "solo", "help", "usage", "premium", "x", "shell",
        "plugin", "plugins", "confirm", "blacklist", "unblacklist", "debug",
    }
)


# discord.py 2.6+ shards itself. Older interpreters used by some local test
# runs only have commands.Bot; those stay on one gateway connection.
_BotBase = getattr(commands, "AutoShardedBot", commands.Bot)




class _MaxwellBotRef:
    """Late-bound stand-in for ``bot.MaxwellBot`` inside the split mixins.

    Methods moved to bot_parts still say ``MaxwellBot._method(self, ...)``.
    This object resolves that attribute on the real class at call time,
    so monkeypatching MaxwellBot attributes keeps working unchanged.
    """

    def __getattr__(self, name):
        import bot

        return getattr(bot.MaxwellBot, name)


MaxwellBot = _MaxwellBotRef()

__all__ = [name for name in dir() if not name.startswith('__')]
