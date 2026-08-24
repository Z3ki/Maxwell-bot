"""Prompt/output protections against context echo loops."""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

_CODE_FENCE = re.compile(r"(```[\\s\\S]*?```|~~~[\\s\\S]*?~~~)")
_WORD = r"[\\w\\u00C0-\\u024F]+(?:['’][\\w\\u00C0-\\u024F]+)?"


def _scrub_prose(text: str, *, max_ngram: int = 12) -> str:
    """Scrub repetition in prose; called only outside fenced code blocks."""
    text = re.sub(r"(?i)(?:j[aeiou]|h(?:a|e)){3,}", lambda m: m.group(0)[:2], text)
    text = re.sub(r"([!?.。，、！？…~])\\1{2,}", r"\\1\\1\\1", text)
    text = re.sub(r"([^\\s\\w])\\1{3,}", r"\\1\\1\\1", text)
    text = re.sub(rf"(?i)\\b({_WORD})\\s+\\1\\b", r"\\1", text)
    separators = r"(?:\\s+|[ \\t]*[,;:—-][ \\t]*|[ \\t]*\\n[ \\t]*)"
    for size in range(max_ngram, 1, -1):
        pattern = re.compile(rf"(?i)(?P<unit>(?:{_WORD}{separators}){{{size-1}}}{_WORD}){separators}(?P=unit)(?=(?:[.!?…]|$))")
        text = pattern.sub(r"\\g<unit>", text)
    return text


def scrub_repetitions(text: str, *, max_ngram: int = 12) -> str:
    """Clean post-generation repetition without modifying fenced code blocks."""
    if not text:
        return text
    parts = _CODE_FENCE.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _scrub_prose(parts[index], max_ngram=max_ngram)
    return "".join(parts)


def sanitize_transcript(messages: Iterable[dict], *, max_chars: int = 120_000) -> list[dict]:
    """Normalize whitespace and remove adjacent duplicate turns before trimming."""
    result: list[dict] = []
    previous = None
    for message in messages:
        role = str(message.get("role", "user"))
        content = re.sub(r"\\s+", " ", str(message.get("content", "")).strip())
        if not content: continue
        key = (role, content.casefold())
        if key == previous: continue
        result.append({**message, "role": role, "content": content})
        previous = key
    total = sum(len(str(m["content"])) for m in result)
    while total > max_chars and result:
        removed = result.pop(0); total -= len(str(removed["content"]))
    return result


def repetition_ratio(text: str, n: int = 3) -> float:
    words = re.findall(r"[\w']+", text.lower())
    if len(words) < n * 2: return 0.0
    grams = [tuple(words[i:i+n]) for i in range(len(words)-n+1)]
    return 1 - (len(set(grams)) / len(grams))


def break_echo_loop(text: str, *, threshold: float = .55) -> str:
    """Truncate at a repeated n-gram boundary, preserving a useful prefix."""
    if repetition_ratio(text) < threshold: return text
    words = text.split(); seen: Counter[tuple[str, ...]] = Counter()
    for i in range(len(words)-2):
        gram = tuple(words[i:i+3]); seen[gram] += 1
        if seen[gram] >= 2: return " ".join(words[:i+3]).rstrip() + " …"
    return text


def sampling_params(config: object) -> dict[str, float]:
    """Return supported provider penalties without sending null/unknown values."""
    result = {}
    for name in ("frequency_penalty", "presence_penalty"):
        value = getattr(config, name, None)
        if value is not None: result[name] = float(value)
    return result
