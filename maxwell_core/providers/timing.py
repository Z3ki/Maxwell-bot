"""Provider-independent latency, token throughput, and debug formatting."""

import time

from .usage import _normalize_llm_usage, _estimate_completion_tokens


def compute_llm_timing(
    *,
    request_start: float,
    first_token_s: float | None,
    last_token_s: float | None = None,
    ended_at: float | None = None,
    headers_ms: float = 0.0,
    usage: dict | None = None,
    endpoint: str = "",
    model: str = "",
    stream: bool = False,
    content_chars: int = 0,
    tool_calls: int = 0,
    content: str = "",
    reasoning: str = "",
    tool_call_payloads=None,
) -> dict:
    """TTFT, generation window, and tokens/sec for one provider call.

    Streaming TTFT is time-to-first-generated-output (content, reasoning, or a
    tool-call delta), not the role-only SSE opener.

    TPS formulas:
    * Single turn (non-stream, or one SSE burst):
      ``generation_time = (duration_ms - ttft_ms) / 1000``;
      ``tps = output_tokens / generation_time``.
      When TTFT equals duration (no visible first token), generation_time
      falls back to the full request so TPS is still defined.
    * Chunk streams (last chunk after first):
      ``(output_tokens - 1) / (t_last_chunk - t_first_chunk)``.

    When the provider omits usage, completion tokens are estimated from the
    output text (~4 chars/token) and marked as such.
    """
    ended = float(ended_at) if ended_at is not None else time.perf_counter()
    total_ms = max(0.0, (ended - float(request_start)) * 1000.0)
    if first_token_s is not None:
        ttft_ms = max(0.0, (float(first_token_s) - float(request_start)) * 1000.0)
    else:
        ttft_ms = total_ms
    tail_ms = max(0.0, total_ms - ttft_ms)
    chunk_stream = False
    if stream and last_token_s is not None and first_token_s is not None:
        decode_span_ms = max(0.0, (float(last_token_s) - float(first_token_s)) * 1000.0)
        chunk_stream = decode_span_ms > 0
    else:
        decode_span_ms = 0.0
    # Chunk streams: last-first (excludes the usage-chunk drain).
    # Single turn / one-shot burst: duration - TTFT.
    gen_ms = decode_span_ms if chunk_stream else tail_ms
    normalized = _normalize_llm_usage(usage)
    prompt = normalized["prompt_tokens"]
    completion = normalized["completion_tokens"]
    total_tok = normalized["total_tokens"]
    tokens_estimated = False
    if completion <= 0:
        estimated = _estimate_completion_tokens(
            content or "",
            reasoning or "",
            tool_call_payloads,
        )
        if estimated <= 0 and content_chars:
            estimated = max(1, (int(content_chars) + 3) // 4)
        if estimated > 0:
            completion = estimated
            total_tok = prompt + completion
            tokens_estimated = True
    if chunk_stream:
        tps = _decode_tps(completion, gen_ms, exclude_first=True)
    else:
        window_ms = gen_ms if gen_ms > 0 else total_ms
        tps = _decode_tps(completion, window_ms, exclude_first=False)
    try:
        headers_val = float(headers_ms or 0.0)
    except (TypeError, ValueError):
        headers_val = 0.0
    return {
        "ts": time.time(),
        "endpoint": str(endpoint or ""),
        "model": str(model or ""),
        "stream": bool(stream),
        "ttft_ms": round(ttft_ms, 1),
        "total_ms": round(total_ms, 1),
        "headers_ms": round(headers_val, 1),
        "gen_ms": round(gen_ms, 1),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total_tok,
        "tokens_estimated": tokens_estimated,
        "tps": tps,
        "content_chars": int(content_chars or 0),
        "tool_calls": int(tool_calls or 0),
    }


def format_timing_debug(
    records,
    *,
    extra: list[str] | None = None,
) -> str:
    """Human-readable TTFT / TPS dump for `/debug` and the debug tool."""
    rows = [r for r in (records or []) if isinstance(r, dict)]
    lines = ["llm debug"]
    if not rows:
        lines.append("no calls recorded yet this process")
    else:
        last = rows[-1]
        lines.append("last call")
        lines.extend(_format_timing_row(last, indent="  "))
        window = rows[-8:]
        if len(window) > 1:
            ttfts = [float(r.get("ttft_ms") or 0) for r in window]
            tpss = [float(r["tps"]) for r in window if r.get("tps") is not None]
            lines.append(f"recent {len(window)}")
            if ttfts:
                lines.append(
                    f"  avg ttft {sum(ttfts) / len(ttfts):.0f}ms  "
                    f"(min {min(ttfts):.0f} / max {max(ttfts):.0f})"
                )
            if tpss:
                weighted = _weighted_tps(window)
                if weighted is not None:
                    lines.append(
                        f"  tps {weighted:.1f}  "
                        f"(min {min(tpss):.1f} / max {max(tpss):.1f})"
                    )
                else:
                    lines.append(
                        f"  tps n/a  (min {min(tpss):.1f} / max {max(tpss):.1f})"
                    )
            for rec in reversed(window[:-1][:5]):
                tps = rec.get("tps")
                tps_s = f"{tps} tps" if tps is not None else "tps n/a"
                lines.append(
                    f"  {float(rec.get('ttft_ms') or 0):.0f}ms ttft  "
                    f"{float(rec.get('total_ms') or 0):.0f}ms  {tps_s}  "
                    f"{rec.get('endpoint') or '?'}"
                )
    if extra:
        lines.extend(str(item) for item in extra if item)
    return "\n".join(lines)


def _decode_tps(
    completion: int, window_ms: float, *, exclude_first: bool
) -> float | None:
    """Tokens/sec over ``window_ms``.

    Chunk streams pass ``exclude_first=True`` so TPS is
    ``(output_tokens - 1) / generation_time``. Single-turn uses the full
    output-token count over ``(duration - TTFT)``.
    """
    if completion <= 0:
        return None
    try:
        window = float(window_ms or 0.0)
    except (TypeError, ValueError):
        return None
    if window <= 0:
        return 0.0 if exclude_first else None
    tokens = float(completion - 1) if exclude_first else float(completion)
    if tokens <= 0:
        return 0.0
    return round(tokens / (window / 1000.0), 1)


def _weighted_tps(records) -> float | None:
    """Aggregate TPS: sum(output_tokens) / sum(generation_time).

    Do not average per-call TPS figures — a 10-token burst and a 90-token
    decode must weight by tokens and generation seconds.
    """
    tokens = 0.0
    seconds = 0.0
    for rec in records or []:
        if not isinstance(rec, dict) or rec.get("tps") is None:
            continue
        try:
            out = float(rec.get("completion_tokens") or 0)
            gen_ms = float(rec.get("gen_ms") or 0)
        except (TypeError, ValueError):
            continue
        if gen_ms <= 0:
            try:
                gen_ms = max(
                    0.0,
                    float(rec.get("total_ms") or 0) - float(rec.get("ttft_ms") or 0),
                )
            except (TypeError, ValueError):
                continue
        if gen_ms <= 0:
            try:
                gen_ms = float(rec.get("total_ms") or 0)
            except (TypeError, ValueError):
                continue
        if out <= 0 or gen_ms <= 0:
            continue
        tokens += out
        seconds += gen_ms / 1000.0
    if tokens <= 0 or seconds <= 0:
        return None
    return tokens / seconds


def format_timing_reply_line(rec: dict | None) -> str:
    """Compact Discord subtext: ``-# ttft 200ms · 49.0 tps``."""
    if not isinstance(rec, dict) or not rec:
        return ""
    parts: list[str] = []
    try:
        ttft = rec.get("ttft_ms")
        if ttft is not None:
            parts.append(f"ttft {float(ttft):.0f}ms")
    except (TypeError, ValueError):
        pass
    tps = rec.get("tps")
    if tps is not None:
        parts.append(f"{tps} tps")
    if not parts:
        return ""
    return "-# " + " · ".join(parts)


def append_timing_to_reply(text: str, rec: dict | None, *, limit: int = 1900) -> str:
    """User-facing replies no longer get a TTFT/TPS footer. `/debug` still has it."""
    return str(text or "")


def _format_timing_row(rec: dict, indent: str = "") -> list[str]:
    tps = rec.get("tps")
    tps_s = f"{tps}" if tps is not None else "n/a"
    if rec.get("tokens_estimated") and tps is not None:
        tps_s = f"{tps_s} est"
    ep = rec.get("endpoint") or "?"
    model = rec.get("model") or "?"
    try:
        headers_val = float(rec.get("headers_ms") or 0)
    except (TypeError, ValueError):
        headers_val = 0.0
    ttft_s = f"ttft {float(rec.get('ttft_ms') or 0):.0f}ms"
    if headers_val >= 1.0:
        ttft_s += f" (headers {headers_val:.0f}ms)"
    return [
        f"{indent}{ep}  {model}",
        (
            f"{indent}{ttft_s}  "
            f"total {float(rec.get('total_ms') or 0):.0f}ms  "
            f"gen {float(rec.get('gen_ms') or 0):.0f}ms"
        ),
        (
            f"{indent}tokens {rec.get('prompt_tokens', 0)} in / "
            f"{rec.get('completion_tokens', 0)} out  tps {tps_s}"
        ),
    ]
