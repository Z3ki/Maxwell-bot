"""Redact secrets and bound tool arguments in diagnostic traces."""

from __future__ import annotations

import os
import re
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(?i)^(?:[a-z0-9]+[_-])*(api[_-]?key|token|password|secret|authorization|cookie|session|"
    r"passwd|private[_-]?key|access[_-]?token|refresh[_-]?token|bearer)$"
)

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|password|secret)[\"']?\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|\S+))"
)

def redact_diagnostics(text: str) -> str:
    """Keep configured credentials out of model prompts and public crash reports."""
    for key, value in os.environ.items():
        if _SECRET_KEY_RE.match(key) and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    return _SECRET_VALUE_RE.sub("[redacted]", text)

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
