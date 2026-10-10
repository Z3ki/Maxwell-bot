"""Tool failure isolation and argument normalization before dispatch."""

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


def is_permission_denied_result(result: str) -> bool:
    """Channel/user access denials do not indicate a globally broken tool."""
    return str(result).lstrip().upper().startswith(
        ("ERROR: MISSING PERMISSIONS", "ERROR - PERMISSION DENIED")
    )


class ToolCircuitBreaker:
    """Track tool failures and temporarily disable failing tools."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        *,
        clock: Callable[[], float] | None = None,
    ):
        self._failures: dict[str, list[float]] = {}
        self._open_until: dict[str, float] = {}
        self.threshold = failure_threshold
        self.recovery = recovery_seconds
        self._clock = clock or time.monotonic

    def record_failure(self, name: str):
        now = self._clock()
        if name not in self._failures:
            self._failures[name] = []
        self._failures[name].append(now)
        # Keep only failures from the last 60 seconds
        self._failures[name] = [t for t in self._failures[name] if now - t < 60]
        if len(self._failures[name]) >= self.threshold:
            self._open_until[name] = now + self.recovery
            logger.warning(
                "Tool circuit breaker OPEN for %s (failures=%d, backoff=%.0fs)",
                name,
                len(self._failures[name]),
                self.recovery,
            )

    def record_success(self, name: str):
        self._failures.pop(name, None)
        self._open_until.pop(name, None)

    def is_open(self, name: str) -> bool:
        until = self._open_until.get(name, 0)
        if until and self._clock() < until:
            return True
        if until:
            self._open_until.pop(name, None)
        return False


def _prepare_tool_params(name: str, params: dict | None) -> dict:
    """Drop kwargs that collide with ``tool.execute(message, **params)``.

    Models alias ``send_message`` as ``message`` and then pass ``message=``
    as the body. That becomes ``execute(discord_message, message=...)``
    which TypeErrors. Same for a leftover ``self``.
    """
    out = dict(params or {})
    leftover_message = out.pop("message", None)
    out.pop("self", None)
    if name == "send_message" and not str(out.get("content") or "").strip():
        for alt in (leftover_message, out.pop("text", None), out.pop("body", None)):
            if alt is not None and str(alt).strip():
                out["content"] = alt
                break
    if (
        name == "create_thread"
        and leftover_message not in (None, "")
        and not str(out.get("opening") or "").strip()
    ):
        out["opening"] = leftover_message
    return out
