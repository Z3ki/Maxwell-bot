"""Prompt/output protections against context echo loops."""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

_CODE_FENCE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")
_WORD = r"[\w\u00C0-\u024F]+(?:['’][\w\u00C0-\u024F]+)?"


def _scrub_prose(text: str, *, max_ngram: int = 12) -> str:
    """Scrub repetition in prose; called only outside fenced code blocks."""
    laugh = r"(?:j[aeiou]|h(?:a|e))"
    # Contiguous laugh run -> a single short burst.
    text = re.sub(rf"(?i)(?:{laugh}){{3,}}", lambda m: m.group(0)[:2], text)
    # Parenthesised laugh run (e.g. "(ja)(ja)(ja)") -> two parens, keeping
    # the trailing separator exactly as it was (don't eat a following space).
    # A paren shown 3+ times (e.g. "(ja)(ja)(ja)") -> keep only two. The regex
    # never matches whitespace after the last paren, so a following word keeps
    # its space. The callback re-emits the first two paren groups joined by the
    # single separator that sat between them.
    paren = rf"\([ \t]*{laugh}[ \t]*\)"

    def _keep_two(match: re.Match) -> str:
        raw = match.group(0)
        first = re.match(rf"{paren}", raw)
        if not first:
            return raw
        start = first.end()
        second = re.match(rf"[ \t]*{paren}", raw[start:])
        if not second:
            return first.group(0)
        return first.group(0) + second.group(0)

    text = re.sub(rf"(?i){paren}(?:[ \t]*{paren}){{2,}}", _keep_two, text)
    # Over-repeated punctuation -> keep exactly three.
    text = re.sub(r"([!?.。，、！？…~])\1{2,}", r"\1\1\1", text)
    text = re.sub(r"([^\s\w])\1{3,}", r"\1\1\1", text)
    # Duplicate adjacent word ("y y" / "de de") -> one.
    text = re.sub(rf"(?i)\b({_WORD})\s+\1\b", r"\1", text)
    # Repeated sentence ("This. This.") -> one. A sentence is words ending in
    # terminal punctuation; dedup adjacent identical sentences.
    sent = rf"{_WORD}(?:[ \t]+{_WORD})*"
    text = re.sub(
        rf"(?i)(?P<sent>{sent}[\.!?…])\s*(?P=sent)(?:\s*(?P=sent))*",
        r"\g<sent>",
        text,
    )
    # Repeated n-gram phrase (2+ occurrences of the same n words joined by
    # whitespace/comma/semicolon) -> one occurrence. Run longest first so a long
    # repeated phrase collapses before its sub-phrases are re-matched. Terminal
    # punctuation is intentionally NOT a separator here (the sentence rule above
    # owns that case, and mixing '.' into this loop breaks the backreference).
    separators = r"(?:\s+|[ \t]*[,;:—-][ \t]*|[ \t]*\n[ \t]*)"
    for size in range(max_ngram, 1, -1):
        unit = rf"(?:{_WORD}{separators}){{{size-1}}}{_WORD}"
        pattern = re.compile(rf"(?i)(?P<unit>{unit})(?:{separators}(?P=unit))+")
        text = pattern.sub(lambda m: m.group("unit"), text)
    # Collapse 3+ blank lines to 2, but never inside code (caller splits).
    text = re.sub(r"\n{3,}", "\n\n", text)
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
        content = re.sub(r"\s+", " ", str(message.get("content", "")).strip())
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
