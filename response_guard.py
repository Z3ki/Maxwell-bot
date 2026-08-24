"""Prompt/output protections against context echo loops."""
from __future__ import annotations
import re
from collections import Counter
from typing import Iterable

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
    words = re.findall(r"[\\w']+", text.lower())
    if len(words) < n * 2: return 0.0
    grams = [tuple(words[i:i+n]) for i in range(len(words)-n+1)]
    return 1 - (len(set(grams)) / len(grams))

def break_echo_loop(text: str, *, threshold: float = .55) -> str:
    """Truncate at a repeated n-gram boundary, preserving a useful prefix."""
    if repetition_ratio(text) < threshold: return text
    words = text.split(); seen: Counter[tuple[str, ...]] = Counter()
    for i in range(len(words)-2):
        gram = tuple(words[i:i+3].copy())
        seen[gram] += 1
        if seen[gram] >= 2: return " ".join(words[:i+3]).rstrip() + " …"
    return text

def sampling_params(config: object) -> dict[str, float]:
    """Return supported provider penalties without sending null/unknown values."""
    result = {}
    for name in ("frequency_penalty", "presence_penalty"):
        value = getattr(config, name, None)
        if value is not None: result[name] = float(value)
    return result
