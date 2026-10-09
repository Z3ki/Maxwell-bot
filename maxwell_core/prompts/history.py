"""Deterministic transcript cleanup; never summarize or change live tool calls."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from media_payloads import strip_media_payloads
from .app_defaults import LEGACY_APP_INSTRUCTIONS

_STAMP = re.compile(r"^\[at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC\]\s*")
_URL = re.compile(r"https?://[^\s<>\[\]\"']+")
_ANNOTATION = re.compile(
    r"\[(?:attachments:|audio:|video:|image:|file:|voice message:|media URL:|embeds:)"
)
_MANIFEST_LINE = re.compile(r"(?:\d+\. [^\n]+\([^\n]+\) — |Source URL: )$")


def compact_media_annotations(text: str) -> str:
    """Keep signed URLs intact once, omitting repeats only in media annotations."""
    seen: set[str] = set()

    def replace(match):
        url = match.group().rstrip(",.;!?) }")
        repeated = url in seen
        seen.add(url)
        # Repeated URLs in actual prose/code belong to the user. Only remove
        # copies in the generated, bracketed media descriptions.
        start = text.rfind("[", 0, match.start())
        inside = start >= 0 and text.rfind("]", 0, match.start()) < start
        line_start = text.rfind("\n", 0, match.start()) + 1
        manifest = _MANIFEST_LINE.fullmatch(text[line_start : match.start()])
        if repeated and (inside and _ANNOTATION.match(text, start) or manifest):
            return "(URL above)" + match.group()[len(url) :]
        return match.group()

    return _URL.sub(replace, text)


def _timestamp(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def history_text(row: dict) -> str:
    """Clean old harness wrappers without mutating the stored row or user prose."""
    text = strip_media_payloads(str(row.get("content") or ""))
    if row.get("prompt_format_version") == 2:
        return compact_media_annotations(text)
    stamp = _STAMP.match(text)
    stored = _timestamp(row.get("timestamp"))
    if (
        stamp
        and stored
        and stored.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") == stamp[1]
    ):
        text = text[stamp.end() :]
    prefix, marker, request = text.partition("\n\nUser request:\n")
    lines = prefix.splitlines()
    known = LEGACY_APP_INSTRUCTIONS | {
        "Give a focused answer with enough explanation to be useful."
    }
    wrapper = any(
        line in known
        and line.startswith(
            (
                "Decide whether this request",
                "Search the web before",
                "Do not use web_search or fetch_url",
            )
        )
        for line in lines
    )
    if (
        marker
        and wrapper
        and all(
            line in known
            or re.fullmatch(r"(?:Respond in |Target language: )[^\n]{1,80}\.", line)
            for line in lines
        )
    ):
        text = request
    return compact_media_annotations(text)


def speaker_key(author_id: str, known: dict[str, str]) -> str:
    """Assign compact keys in transcript order; appending rows preserves keys."""
    return known.setdefault(author_id, f"u{len(known) + 1}")


def canonical_history(
    rows: list[dict], self_id: str, self_names: set[str], *, channel_id: str = ""
) -> list[dict]:
    """Use message IDs for dedup; migrate only identifiable legacy send echoes."""
    unique: dict[str, int] = {}
    cleaned: list[dict] = []
    for row in rows:
        item = {**row, "content": history_text(row), "prompt_format_version": 2}
        mid = str(row.get("message_id") or "")
        if mid and mid in unique:
            cleaned[unique[mid]] = item
        else:
            if mid:
                unique[mid] = len(cleaned)
            cleaned.append(item)

    def is_self(row):
        aid = str(row.get("author_id") or "")
        return (bool(self_id) and aid == self_id) or (
            not aid and row.get("author") in self_names
        )

    by_text: dict[str, list[dict]] = {}
    for row in cleaned:
        if not row.get("is_tool") and is_self(row):
            by_text.setdefault(row["content"].strip(), []).append(row)

    def echo_candidates(row, content, *, include_synthetic=False):
        stamp = _timestamp(row.get("timestamp"))
        if stamp is None:
            return []
        matches = []
        for other in by_text.get(content.strip(), []):
            if other is row:
                continue
            mid = str(other.get("message_id") or "")
            eligible = (
                mid.isdecimal()
                or include_synthetic
                and mid.startswith("bot_send_message:")
            )
            other_stamp = _timestamp(other.get("timestamp"))
            if (
                eligible
                and other_stamp
                and abs((stamp - other_stamp).total_seconds()) <= 5
            ):
                matches.append(other)
        return matches

    # Each actual delivery can account for one tool call. Two intentional
    # calls with the same text must never collapse into a single old echo.
    redundant = {
        id(row)
        for row in cleaned
        if str(row.get("message_id") or "").startswith("bot_send_message:")
        and is_self(row)
        and echo_candidates(row, row["content"])
    }
    used_deliveries: set[int] = set()
    result = []
    for row in cleaned:
        if id(row) in redundant:
            continue
        if row.get("is_tool") and row.get("tool_name") == "send_message":
            output = str(row.get("tool_result") or row["content"])
            marker = "__MESSAGE_SENT__\n"
            if marker in output:
                destination = str(
                    (row.get("tool_params") or {}).get("channel_id") or channel_id
                )
                if channel_id and destination != channel_id:
                    result.append(
                        {
                            **row,
                            "content": f"send_message to {destination}: __MESSAGE_SENT__",
                        }
                    )
                    continue
                content = compact_media_annotations(output.split(marker, 1)[1].strip())
                # A legacy tool log may be the only record of a second send.
                candidates = [
                    other
                    for other in echo_candidates(row, content, include_synthetic=True)
                    if id(other) not in redundant and id(other) not in used_deliveries
                ]
                if candidates:
                    used_deliveries.add(id(candidates[0]))
                elif content:
                    result.append(
                        {
                            **row,
                            "is_tool": False,
                            "author": next(iter(sorted(self_names)), "Maxwell"),
                            "author_id": self_id,
                            "content": content,
                        }
                    )
                continue
        result.append(row)
    return result
