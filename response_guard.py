"""Prompt/output protections against context echo loops."""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

_CODE_FENCE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")
_WORD = r"[\w\u00C0-\u024F]+(?:['\u2019][\w\u00C0-\u024F]+)?"
_WORD_RE = re.compile(_WORD)
_SEPARATORS = r"(?:\s+|[ \t]*[,;:\u2014-][ \t]*|[ \t]*\n[ \t]*)"
_TERMINATORS = ".!?\u2026"
# Sentence boundaries are found by scanning for the terminators themselves.
# A "[^.!?]*[.!?]" chunk pattern reads better and is a trap: on a stretch of
# prose that never reaches a full stop it rescans from every position, which
# is O(n**2) — 3.8s on one 19KB message.
_TERMINATOR_RE = re.compile(rf"[{re.escape(_TERMINATORS)}]")


def _is_plain_sentence(body: str) -> bool:
    """A run of words and single spaces ending in terminal punctuation.

    Deliberately narrow, and the same shape the old regex recognized: no
    commas, no newlines, no list bullets. "- Item." twice in a list is a list,
    not a stutter.
    """
    if len(body) < 2 or body[-1] not in _TERMINATORS:
        return False
    core = body[:-1]
    if not any(ch.isalnum() for ch in core):
        return False
    return all(ch.isalnum() or ch in "_ \t'\u2019" for ch in core)


def _collapse_repeated_sentences(text: str) -> str:
    r""""This works. This works." -> "This works."

    This used to be one regex with a backreference over an unbounded
    `word(\s+word)*` run, which backtracks catastrophically on any long
    stretch of prose that never reaches a full stop: ~4s on an 8KB message,
    burned in the event loop, on every single reply. Same rule, one linear
    pass, no backtracking.
    """
    if not text:
        return text
    kept: list[str] = []
    previous = ""
    end = 0
    for match in _TERMINATOR_RE.finditer(text):
        chunk = text[end : match.end()]
        end = match.end()
        body = chunk.strip()
        if previous and body.casefold() == previous and _is_plain_sentence(body):
            # Drop the repeat AND the whitespace that led to it, exactly as
            # the old pattern's `\s*` did.
            continue
        kept.append(chunk)
        previous = body.casefold() if _is_plain_sentence(body) else ""
    kept.append(text[end:])
    return "".join(kept)


def _has_adjacent_repeat(words: list[str], size: int) -> bool:
    """True when some run of `size` words is immediately followed by itself."""
    for i in range(len(words) - 2 * size + 1):
        if words[i : i + size] == words[i + size : i + 2 * size]:
            return True
    return False


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
    # Repeated sentence ("This. This.") -> one.
    text = _collapse_repeated_sentences(text)
    # Repeated n-gram phrase (2+ occurrences of the same n words joined by
    # whitespace/comma/semicolon) -> one occurrence. Run longest first so a long
    # repeated phrase collapses before its sub-phrases are re-matched. Terminal
    # punctuation is intentionally NOT a separator here (the sentence rule above
    # owns that case, and mixing '.' into this loop breaks the backreference).
    words = _WORD_RE.findall(text.lower())
    for size in range(max_ngram, 1, -1):
        # The regex below is the expensive part of this whole module, and on
        # ordinary prose it finds nothing. Ask a linear question first — does
        # any run of `size` words repeat back to back at all? — and only pay
        # for the regex when the answer is yes. The check is a necessary
        # condition for the pattern to match, so nothing that used to be
        # collapsed stops being collapsed.
        if not _has_adjacent_repeat(words, size):
            continue
        unit = rf"(?:{_WORD}{_SEPARATORS}){{{size-1}}}{_WORD}"
        pattern = re.compile(rf"(?i)(?P<unit>{unit})(?:{_SEPARATORS}(?P=unit))+")
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
        if not content:
            continue
        key = (role, content.casefold())
        if key == previous:
            continue
        result.append({**message, "role": role, "content": content})
        previous = key
    total = sum(len(str(m["content"])) for m in result)
    while total > max_chars and result:
        removed = result.pop(0)
        total -= len(str(removed["content"]))
    return result


def repetition_ratio(text: str, n: int = 3) -> float:
    words = re.findall(r"[\w']+", text.lower())
    if len(words) < n * 2:
        return 0.0
    grams = [tuple(words[i:i+n]) for i in range(len(words)-n+1)]
    return 1 - (len(set(grams)) / len(grams))


def break_echo_loop(text: str, *, threshold: float = .55) -> str:
    """Truncate at a repeated n-gram boundary, preserving a useful prefix.

    Fenced code is left alone entirely. Repetition is what an echo loop looks
    like, but it is also what a table, a list of assertions or a block of
    JSON looks like, and truncating one of those mid-fence corrupts the code
    AND leaves the fence unterminated. `scrub_repetitions` has always skipped
    fences; this ran after it over the whole reply and undid that, so a code
    block with a dozen similar lines came out cut to its first two.
    """
    if _CODE_FENCE.search(text):
        return text
    if repetition_ratio(text) < threshold:
        return text
    words = text.split()
    seen: Counter[tuple[str, ...]] = Counter()
    for i in range(len(words)-2):
        gram = tuple(words[i:i+3])
        seen[gram] += 1
        if seen[gram] >= 2:
            return " ".join(words[:i+3]).rstrip() + " …"
    return text


def sampling_params(config: object) -> dict[str, float]:
    """Return supported provider penalties without sending null/unknown values."""
    result = {}
    for name in ("frequency_penalty", "presence_penalty"):
        value = getattr(config, name, None)
        if value is not None:
            result[name] = float(value)
    return result
