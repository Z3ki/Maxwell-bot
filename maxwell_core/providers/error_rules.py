"""Recognize deterministic compatibility errors without owning retry policy."""

import re


def context_output_limit(status: int, error_text: str, current: int) -> int | None:
    """Recover an output budget from a provider's context-overflow counts.

    The clamp belongs to this request and endpoint: another endpoint may have
    a larger context, and a later request has different input token counts.
    """
    if status != 400:
        return None
    context = re.search(r"maximum context length is (\d+) tokens", error_text, re.I)
    requested = re.search(r"you requested about (\d+) tokens", error_text, re.I)
    if context is None or requested is None:
        return None
    try:
        context_tokens = int(context.group(1))
        requested_tokens = int(requested.group(1))
    except ValueError:
        return None
    if context_tokens <= 0 or requested_tokens < current:
        return None
    available = context_tokens - (requested_tokens - current)
    if available <= 1:
        return None
    # Keep the existing 512-token reserve for large contexts. With a small
    # remaining window, reserve a fraction instead of imposing a 4096 floor
    # that guarantees the corrected request will overflow again.
    reserve = min(512, max(1, available // 8))
    safe = available - reserve
    return safe if 0 < safe < current else None


def maximum_output_limit(status: int, error_text: str, current: int) -> int | None:
    """Recover a deterministic per-model output cap without changing fallback."""
    if status != 400:
        return None
    match = re.search(r"maximum output tokens\s*\(?\s*(\d+)\s*\)?", error_text, re.I)
    if match is None:
        return None
    try:
        cap = int(match.group(1))
    except ValueError:
        return None
    safe = max(1, cap - 64)
    return safe if cap > 0 and safe < current else None


def _is_usage_exhausted_error(status: int, error_text: str) -> bool:
    """Detect true quota/credit exhaustion — not ordinary rate limits.

    Transient 429 rate limits must still get normal retry/backoff. Only treat as
    exhausted when the body clearly indicates cooldown, quota, or credits.

    2026-08-30 fix for Google Antigravity pooled false positive:
    - Antigravity-manager pools 5 Google accounts; a single 429 with
      reason=QuotaExhausted for gemini-3-flash on ONE account is NOT global
      exhaustion — combined quota may still be 70% (observed 2026-08-30).
      The manager still serves other accounts/models, so the provider must
      treat this as transient and fall back, not raise USAGE_EXHAUSTED.
    - Google's error is "QuotaExhausted" (no space) not "quota exceeded",
      so the old marker list missed it (false negative) while also flagging
      single-model hits as global (false positive). Both are fixed here.
    """
    text = (error_text or "").lower()
    # Explicit exhaustion / cooldown markers (avoid bare "usage" / "rate limit").
    markers = (
        "model_cooldown",
        "cooling down",
        "insufficient_quota",
        "insufficient credits",
        "credit balance",
        "quota exceeded",
        "quotaexhausted",  # Google Antigravity: QuotaExhausted (no space)
        "quota_exhausted",
        "resource_exhausted",
        "resource exhausted",
        "out of credits",
        "out of quota",
        "billing hard limit",
        "spend limit",
    )
    if status != 429:
        return False
    is_rate_limit = (
        "rate limit" in text or "rate_limit" in text or "too many requests" in text
    )
    is_quota_marker = any(m in text for m in markers)
    if is_rate_limit and not is_quota_marker:
        return False
    # Antigravity pooled false-positive guard: single-model QuotaExhausted
    # (e.g. gemini-3-flash, gemini-2.5-pro) on one pooled account should be
    # transient, not global. Only treat as global exhausted if the error
    # carries a stronger billing/credit signal or no specific model is named.
    if is_quota_marker:
        # If the text names a specific Gemini/Claude model, it's likely per-model
        # cooldown from the pool, not the whole API being drained.
        # "pro" also matched "provider" and "project" and turned global
        # exhaustion into an ordinary retry. A quota error code alone does
        # not identify a particular account/model in a pooled service.
        has_model = bool(re.search(r"\b(?:gemini|claude)(?:\b|[-\d.])", text))
        has_global = any(
            g in text
            for g in (
                "billing",
                "credit",
                "insufficient",
                "out of",
                "spend limit",
                "model_cooldown",
                "cooling down",
            )
        )
        if has_model and not has_global and not is_rate_limit:
            # Single entry like 'QuotaExhausted for gemini-3-flash' — transient, fall back to Grok/other model
            # unless the payload explicitly says combined/global is exhausted.
            # Check for combined/global hint: if manager said so, it would mention billing or multiple accounts
            return False
        if has_model and is_rate_limit:
            # "rate limited ... QuotaExhausted ... gemini-3-flash" — also transient pooled case
            # Only global if billing/credit is mentioned
            if not has_global:
                return False
    return is_quota_marker


def _is_policy_block_text(text: str) -> bool:
    """True when a 200-OK *reply body* is actually Gemini's prompt-block notice.

    z3ki (and Google's OpenAI-compat surface) do not return an HTTP error for a
    blocked prompt — they hand back a normal 200 whose message content is:

        The prompt could not be submitted. The prompt contains sensitive words
        that violate Google's (...use-policy). Try rephrasing the prompt. ...

    Nothing upstream flags it, so Maxwell relayed it into the channel verbatim
    (logged 2026-08-21, #villa-31 and #poketwo-spawns). These markers are the
    provider's own boilerplate; a genuine reply does not contain them. A false
    positive only costs us one turn answered by the fallback model, so this is
    deliberately eager.
    """
    t = (text or "").lower()
    return any(
        m in t
        for m in (
            "the prompt could not be submitted",
            "contains sensitive words",
            "policies.google.com/terms/generative-ai/use-policy",
            "ai.google.dev/gemini-api/docs/troubleshooting",
        )
    )


def _is_content_policy_block(status: int, error_text: str) -> bool:
    """True when the provider refused the *prompt* on content-policy grounds.

    Gemini (and OpenAI-compatible proxies in front of it) reject the request
    outright rather than returning a completion, e.g.

        The prompt could not be submitted. The prompt contains sensitive words
        that violate Google's use policy. Try rephrasing the prompt.

    The native API signals the same thing as promptFeedback.blockReason
    (PROHIBITED_CONTENT / BLOCKLIST / SPII / SAFETY). None of it is transient:
    retrying the identical payload against the same endpoint always loses, so
    this cools the endpoint and fails straight over to the fallback model.
    """
    text = (error_text or "").lower()
    if status not in (400, 403, 422, 451, 200):
        return False
    markers = (
        "sensitive words",
        "could not be submitted",
        "generative-ai/use-policy",
        "prohibited_content",
        "blocked_reason",
        "blockreason",
        "safety_ratings",
        "content policy",
        "content_policy",
        "content_filter",
        "responsibleaipolicyviolation",
    )
    return any(m in text for m in markers)


def _is_media_unsupported_error(status: int, error_text: str) -> bool:
    """True when the endpoint rejected image/video/audio content parts."""
    text = (error_text or "").lower()
    if status == 404 and "support input audio" in text:
        return True
    if status in (400, 404) and (
        "unknown variant `image_url`" in text
        or "unknown variant `video_url`" in text
        or "unknown variant `input_audio`" in text
        or ("expected `text`" in text and "image_url" in text)
    ):
        return True
    # OpenRouter phrases a text-only routing failure as a bare 404:
    #   {"error":{"message":"No endpoints found that support image input"}}
    # This has no `image_url` token in it, so the checks above missed it and
    # every image turn hard-failed instead of falling back (logged 2026-08-12).
    if status in (400, 404) and "no endpoints found that support" in text:
        return True
    # Generic provider phrasings: "model does not support image input",
    # "does not support images", "image input is not supported".
    if status in (400, 404, 415, 422):
        for media_word in ("image", "images", "audio", "video", "multimodal"):
            if (
                f"not support {media_word}" in text
                or f"{media_word} input is not supported" in text
                or f"{media_word} input not supported" in text
            ):
                return True
    return False


# "invalid temperature: only 0.6 is allowed for this model" (Console Go via
# OpenRouter). Deterministic — retrying the same payload burns every attempt
# and then falls back for no reason, so parse the demanded value and resend.
_TEMPERATURE_CONSTRAINT_RE = re.compile(
    r"temperature[^.]{0,80}?only\s+([0-9]*\.?[0-9]+)\s+is\s+allowed",
    re.IGNORECASE,
)
_TEMPERATURE_RANGE_RE = re.compile(
    r"temperature[^.]{0,80}?(?:must be|should be)[^.]{0,40}?"
    r"(?:between|in)\s+\[?\s*([0-9]*\.?[0-9]+)\s*(?:,|and|-)\s*([0-9]*\.?[0-9]+)",
    re.IGNORECASE,
)


def _required_temperature(status: int, error_text: str) -> float | None:
    """Extract the temperature an endpoint demands from a 400 body."""
    if status != 400:
        return None
    text = error_text or ""
    if "temperature" not in text.lower():
        return None
    match = _TEMPERATURE_CONSTRAINT_RE.search(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    match = _TEMPERATURE_RANGE_RE.search(text)
    if match:
        try:
            low, high = float(match.group(1)), float(match.group(2))
        except ValueError:
            return None
        if low > high:
            low, high = high, low
        # Aim at the middle of the accepted band rather than an endpoint,
        # which providers sometimes treat as exclusive.
        return round((low + high) / 2, 3)
    return None


def _is_stream_options_rejected(status: int, error_text: str) -> bool:
    if status not in (400, 422):
        return False
    text = (error_text or "").lower()
    return "stream_options" in text or "include_usage" in text
