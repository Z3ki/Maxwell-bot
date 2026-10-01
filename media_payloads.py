"""Keep binary tool payloads out of text history and retain turn-local media."""

import re


_MEDIA_PAYLOAD_RE = re.compile(
    r"__(IMAGE|AUDIO)_B64__.*?(?:__END_\1_B64__|\Z)", re.DOTALL
)


def strip_media_payloads(text: str) -> str:
    """Strip complete and legacy truncated payloads before any text clipping."""
    return _MEDIA_PAYLOAD_RE.sub(
        lambda match: f"[{match.group(1).lower()} payload omitted]", text
    )


def sanitize_media_memory(value):
    """Copy text/metadata without persisting encoded media or mutating callers."""
    if isinstance(value, str):
        return strip_media_payloads(value)
    if isinstance(value, dict):
        return {
            key: "[binary payload omitted]"
            if key == "b64"
            else sanitize_media_memory(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_media_memory(item) for item in value]
    return value


def merge_followup_media(
    original_media: list[dict], tool_media: list[dict], tool_images: list[str]
) -> list[dict]:
    """Carry this turn's attachments through tools, deduplicating binary data.

    Original metadata wins when a tool reattaches the same image. Legacy tool
    images still use the provider's historical PNG default. No channel cache
    is consulted here: a follow-up belongs to this request alone.
    """
    merged = []
    seen = set()
    for item in (
        original_media
        + tool_media
        + [{"b64": encoded, "mime_type": "image/png"} for encoded in tool_images]
    ):
        encoded = item.get("b64")
        if not encoded or encoded in seen:
            continue
        seen.add(encoded)
        merged.append(dict(item))
    return merged
