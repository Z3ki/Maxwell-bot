"""Reasoning plumbing for Maxwell tool calls.

There is no standalone `reasoning_log` tool and no second schema builder. Every
tool carries an OPTIONAL-in-shape, required-in-practice `reasoning` string that
tool_schemas.build_openai_tools() injects into its JSON schema, so the model
does its thinking inside the call it actually wants to make. This module owns
the other half of that contract:

- `extract_reasoning()` pops `reasoning` back out before the params reach
  Tool.execute(), so no tool ever sees the kwarg.
- `record_reasoning()` is the ONE function that persists a trace to the
  dashboard JSON. Both dispatch paths (native + XML) funnel through it. Stop
  adding new places to write `llm_traces.json` — there is one.

Schemas, the result contract, and the tool catalog live in tool_schemas.py.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Max chars we keep from a reasoning string. The trace is shown on a dashboard;
# a novel here helps nobody and bloats the context budget.
REASONING_MAX_CHARS = 280


def _sanitize_reasoning(raw: Any) -> str:
    """Coerce reasoning to a bounded, plain-text string.

    The model occasionally wraps thoughts in <thoughts>...</thoughts> like a
    smartass, or dumps JSON. Strip the tags, cap the length, cut at the first
    sentence terminator inside the cap so we never see half-sentences or
    whole-artifact dumps in the trace, move on. We do NOT try to be clever
    and parse nested payloads — reasoning is one string. If the model can't
    follow that, it gets clamped, not interpreted.

    The 2026-07-19 user complaint: the model was dumping the entire Spanish
    joke site body into the `reasoning` field for create_site, and the user
    saw the whole body on the progress message and the trace. We now cap
    hard at 280 chars AND prefer a clean sentence break inside that window.
    """
    import re

    text = str(raw or "").strip()
    if "<" in text and ">" in text:
        text = re.sub(r"<[^>]+>", " ", text).strip()
    if not text:
        return ""
    if len(text) > REASONING_MAX_CHARS:
        # Cut at the last sentence terminator inside the cap, so a long
        # reasoning that contains a complete sentence + a half-thought
        # shows just the complete sentence. Falls back to a hard cap
        # with ellipsis if no terminator exists in the window.
        head = text[: REASONING_MAX_CHARS - 1]  # leave 1 char for the ellipsis
        last_term = -1
        for m in re.finditer(r"[.!?](?:\s|$)", head):
            last_term = m.end()
        if last_term > 0:
            text = head[:last_term].rstrip()
        else:
            # No sentence break in the window. Cut at the last word
            # boundary so the trace doesn't end on a half-word. The
            # cap is REASONING_MAX_CHARS - 1 chars + "…" so the final
            # length is exactly REASONING_MAX_CHARS.
            cut = head.rfind(" ")
            # Only use the word boundary if it's in the back half of
            # the window — otherwise the cap leaves a tiny fragment.
            if cut > REASONING_MAX_CHARS * 0.5:
                text = head[:cut].rstrip() + "…"
            else:
                text = head.rstrip() + "…"
    return text


async def record_reasoning(
    bot: Any,
    message: Any,
    *,
    tool_name: str,
    reasoning: str,
    params: dict[str, Any],
    result: str,
) -> None:
    """ONE reasoning recorder. Both dispatch paths call this.

    Writes a trace payload keyed by the tool that actually ran (not a phantom
    `reasoning_log` tool), so the dashboard shows reasoning attached to the
    real action. If `reasoning` is empty we still record a stub so every tool
    call is auditable — that's the whole reason this exists.

    Swallows errors: a trace write failure must NEVER abort the tool result
    that already happened. The user already saw the action; losing a trace
    line is fine, losing the reply is not.
    """
    cleaned = _sanitize_reasoning(reasoning)
    payload: dict[str, Any] = {
        "thoughts": cleaned or "(no reasoning provided by the model)",
        "tool": tool_name,
        "params_preview": _summarize_params(params),
    }
    try:
        await bot._record_llm_trace(message, payload)
    except Exception as e:  # noqa: BLE001 — intentional, see docstring
        logger.warning("Failed to record reasoning trace for %s: %s", tool_name, e)


def _summarize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Throw away the giant blobs (HTML bodies, file contents) for the trace.

    The trace is for humans eyeballing reasoning, not a byte-exact replay.
    Keeping a 2MB create_site body in llm_traces.json would be insane.
    """
    out: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if k == "reasoning":
            continue
        if isinstance(v, str) and len(v) > 200:
            out[k] = f"[{len(v)} chars]"
        elif isinstance(v, (list, tuple)) and len(v) > 20:
            out[k] = f"[{len(v)} items]"
        else:
            out[k] = v
    return out


def extract_reasoning(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pop `reasoning` out of a tool's params so it's not passed to execute().

    Returns (reasoning, params_without_reasoning). Tools don't know about
    `reasoning`; it's a registry-level concern. This keeps Tool.execute()
    signatures clean and stops a tool from accidentally `**kwargs`-ing it into
    a real API call somewhere.
    """
    params = dict(params or {})
    reasoning = str(params.pop("reasoning", "") or "")
    return reasoning, params
