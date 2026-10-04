"""Normalize provider token/cost metadata and estimate omitted token counts."""

import json
import math


def _coerce_token_count(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError, OverflowError):
            return 0


def _first_present_token_count(raw: dict, *keys: str) -> int:
    for key in keys:
        if key in raw and raw[key] is not None:
            return _coerce_token_count(raw[key])
    return 0


def _reported_cost_usd(raw) -> float:
    if not isinstance(raw, dict):
        return 0.0
    details = raw.get("cost_details")
    candidates = [raw.get("cost"), raw.get("total_cost")]
    if isinstance(details, dict):
        candidates.extend(
            (details.get("upstream_inference_cost"), details.get("total"))
        )
    for value in candidates:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        try:
            cost = float(value)
        except OverflowError:
            continue
        if math.isfinite(cost):
            return max(0.0, cost)
    return 0.0


def _normalize_llm_usage(raw) -> dict:
    """Map OpenAI / OpenRouter / Ollama usage blobs onto one shape."""
    if not isinstance(raw, dict):
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }
    prompt = _first_present_token_count(
        raw, "prompt_tokens", "input_tokens", "prompt_eval_count"
    )
    completion = _first_present_token_count(
        raw, "completion_tokens", "output_tokens", "eval_count"
    )
    # Preserve extra billable tokens but never report a total smaller than
    # the components already known to have been generated.
    total = max(_first_present_token_count(raw, "total_tokens"), prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost_usd": _reported_cost_usd(raw),
    }


def _estimate_completion_tokens(
    content: str = "",
    reasoning: str = "",
    tool_calls=None,
) -> int:
    """Fallback output-token count when the provider omitted usage (~4 chars/tok)."""
    chunks = [str(content or ""), str(reasoning or "")]
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        chunks.append(str((fn or {}).get("name") or ""))
        args = (fn or {}).get("arguments")
        if isinstance(args, dict):
            try:
                chunks.append(json.dumps(args, ensure_ascii=False))
            except (TypeError, ValueError):
                chunks.append(str(args))
        elif args:
            chunks.append(str(args))
    n = sum(len(part) for part in chunks)
    if n <= 0:
        return 0
    return max(1, (n + 3) // 4)
