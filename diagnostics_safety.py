"""Redact secrets and bound tool arguments in diagnostic traces."""

from __future__ import annotations

import os
import json
import logging
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
import re
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(?i)^(?:[a-z0-9]+[_-])*(api[_-]?key|token|password|secret|authorization|cookie|session|"
    r"passwd|private[_-]?key|access[_-]?token|refresh[_-]?token|bearer)$"
)
_ENV_SECRET_KEY_RE = re.compile(
    r"(?i)token|key|password|passwd|secret|authorization|cookie|session|credential|webhook"
)

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|password|secret)[\"']?\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|\S+))"
)
_DISCORD_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,})\b"
)
_WEBHOOK_RE = re.compile(
    r"(?i)https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/[^\s\"'<>]+"
)

# Values are populated by the active turn and inherited by asyncio child tasks.
log_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)
_LOG_FIELDS = frozenset({
    "trigger", "guild_id", "channel_id", "model", "provider", "duration_ms",
    "success", "tool_name", "queries", "results", "tokens_in", "tokens_out",
    "error_type", "outcome", "correlation_id", "model_calls", "tool_count",
})
_SAFE_FIELD = re.compile(r"^[a-zA-Z0-9_.:/-]{1,100}$")
_MAX_LOG_MESSAGE = 200


def redact_diagnostics(text: str) -> str:
    """Keep configured credentials out of model prompts and public crash reports."""
    secrets = sorted(
        (value for key, value in os.environ.items()
         if value and (_SECRET_KEY_RE.match(key) or _ENV_SECRET_KEY_RE.search(key))),
        key=len, reverse=True,
    )
    for value in secrets:
        text = text.replace(value, "[redacted]")
    text = _WEBHOOK_RE.sub("[redacted webhook]", text)
    text = _DISCORD_CREDENTIAL_RE.sub("[redacted]", text)
    return _SECRET_VALUE_RE.sub("[redacted]", text)

class SafeLogFormatter(logging.Formatter):
    """Format logs without exposing arbitrary application data at INFO or above."""

    def __init__(self, *, json_format: bool = False):
        super().__init__()
        self.json_format = json_format

    def format(self, record: logging.LogRecord) -> str:
        # Arbitrary existing logging calls may contain user content, including in
        # f-strings and exception messages. Only explicitly safe instrumentation
        # may opt into retaining its message at INFO and above.
        if record.levelno < logging.INFO or getattr(record, "safe_log", False) is True:
            try:
                message = redact_diagnostics(record.getMessage())[:_MAX_LOG_MESSAGE]
            except Exception:
                message = "[unavailable]"
        else:
            message = "[message omitted]"
        data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "function": record.funcName,
            "line": record.lineno,
        }
        context = dict(log_context.get() or {})
        for key in _LOG_FIELDS:
            value = getattr(record, key, context.get(key))
            if isinstance(value, (int, float, bool)):
                data[key] = value
            elif isinstance(value, str) and _SAFE_FIELD.fullmatch(value):
                data[key] = redact_diagnostics(value)
        if record.exc_info and record.exc_info[0]:
            data["exception_type"] = record.exc_info[0].__name__
            # Never include exception text or source lines: both may contain
            # prompts, tool results, or credentials.
            data["stack"] = [
                f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(record.exc_info[2])[-20:]
            ]
        if self.json_format:
            return json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        fields = " ".join(
            f"{key}={value}" for key, value in data.items()
            if key not in {"timestamp", "level", "logger", "message"}
        )
        return f"{data['timestamp']} - {data['logger']} - {data['level']} - {message} {fields}"

def sanitize_tool_args(value: Any, *, _depth: int = 0) -> Any:
    """Drop secrets and trim bulky bodies before they hit a prompt or a PR."""
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_val in list(value.items())[:40]:
            key = str(raw_key)
            if _SECRET_KEY_RE.match(key) or key.startswith("_"):
                out[key] = "[redacted]"
            else:
                out[key] = sanitize_tool_args(raw_val, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_tool_args(v, _depth=_depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        text = redact_diagnostics(value)
        limit = 200 if _depth == 0 else 400
        if len(text) > limit:
            return text[:limit] + f"…[{len(value)} chars]"
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_tool_args(str(value), _depth=_depth + 1)
