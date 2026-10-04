"""Tool delta accumulation and provider-specific continuation metadata."""

import json
import re

_PARTIAL_REASONING_RE = re.compile(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _keep_tool_call_provider_fields(slot: dict, tc_delta: dict) -> None:
    """Keep provider fields the next request must echo.

    Gemini rejects a follow-up tool turn when ``thought_signature`` (often
    under ``extra_content.google``) was present on the function call and
    then dropped. Streaming only used to copy id, type, and function.
    """
    if not isinstance(tc_delta, dict):
        return
    for key, value in tc_delta.items():
        if key in {"index", "id", "type", "function"} or str(key).startswith("_"):
            continue
        if value in (None, "", {}, []):
            continue
        if not slot.get(key):
            slot[key] = value
    fn = tc_delta.get("function")
    if not isinstance(fn, dict):
        return
    slot_fn = slot.setdefault("function", {})
    for key, value in fn.items():
        if key in {"name", "arguments"} or str(key).startswith("_"):
            continue
        if value in (None, "", {}, []):
            continue
        if not slot_fn.get(key):
            slot_fn[key] = value


def _append_tool_call_arguments(slot: dict, incoming) -> None:
    """Accumulate streaming tool-call arguments onto ``slot``.

    OpenAI streams ``function.arguments`` as JSON *strings* that must be
    concatenated. Some OpenAI-compatible providers (GLM-5.x on OpenCode
    Zen Go) send a finished object in one delta instead — concatenating
    that with ``""`` raises TypeError and kills the turn.
    """
    fn = slot.setdefault("function", {})
    existing = fn.get("arguments") or ""
    if isinstance(existing, dict):
        existing = json.dumps(existing, ensure_ascii=False)
    if isinstance(incoming, dict):
        fn["arguments"] = json.dumps(incoming, ensure_ascii=False)
        return
    if incoming is None:
        fn["arguments"] = existing
        return
    fn["arguments"] = existing + (
        incoming if isinstance(incoming, str) else str(incoming)
    )


def _extract_partial_reasoning(arguments: str) -> str:
    """Best-effort pull of the `reasoning` string from a PARTIAL arguments JSON.

    Returns '' until the reasoning value's closing quote has arrived (i.e. the
    model is still emitting it). Once complete, returns the decoded string.
    Used to update the in-channel progress message with the model's real intent
    mid-stream, instead of a static "generating…" for the whole generation.
    """
    if not arguments:
        return ""
    if isinstance(arguments, dict):
        r = arguments.get("reasoning")
        return r if isinstance(r, str) else ""
    if not isinstance(arguments, str):
        arguments = str(arguments)
    # Fast path: the whole arguments object already parses.
    try:
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            r = parsed.get("reasoning")
            if isinstance(r, str):
                return r
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # Partial JSON: grab the reasoning value once its closing quote landed.
    m = _PARTIAL_REASONING_RE.search(arguments)
    if not m:
        return ""
    raw = m.group(1)
    try:
        return json.loads('"' + raw + '"')  # decode \n, \", etc.
    except (json.JSONDecodeError, ValueError):
        return raw
