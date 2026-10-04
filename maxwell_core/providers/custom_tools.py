"""Incrementally recover bare-JSON tool calls from assistant text.

The parser preserves surrounding prose and supports a narrow repair of model
emitted HTML strings. It is independent of HTTP, retries, and Discord.
"""

import contextlib
import json
import re

# Matches the opening of a tool call — `{"name": "<tool>"`. We use this to
# find the start position even before we know the full JSON will parse.
_CUSTOM_TOOL_OPEN_RE = re.compile(r'\{\s*"name"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')

# Opener-match failure recovery threshold. If the brace counter can't find
# a balanced close inside this many characters after a `{"name":` match,
# we give up on this opener and look for the next one. Prevents a single
# pathological opener (think: create_site's HTML body with embedded
# unbalanced `'{"name": "...' substrings from a prior tool's args, or
# a stray `"` inside CSS that strands the string-state counter) from
# silently disabling extraction for the rest of the stream.
_GIVE_UP_BYTES = 65536

# When no opener regex match is found in the unreleased buffer, how many
# bytes of recent text we hold back before emitting everything else as
# visible. The opener `{"name": "<value>"` can be up to ~50 chars depending
# on the tool name; 256 chars is comfortably larger and keeps a near-zero
# memory footprint. This is what lets the buffer find a tool call whose
# opener arrives split across many small SSE deltas — without it, the
# leading chunk would be released as visible and the partial opener lost
# forever.
_HOLD_BACK = 256


class _CustomToolCallBuffer:
    """Extract bare-JSON tools while preserving prose and malformed candidates.

    ``feed`` returns only newly released prose. Tool JSON remains hidden from
    progress previews until it has been parsed into ``completed``. Consumed
    prefixes are removed from the working buffer on each feed; ``text_parts``
    owns the visible reply, so long plain replies are not retained twice.
    """

    def __init__(self, on_partial_name=None):
        self._buf = ""
        self._released_len = 0
        self.text_parts: list[str] = []
        self.completed: list[dict] = []
        self._on_partial_name = on_partial_name
        self._announced_names: set[str] = set()

    @property
    def has_pending_json(self) -> bool:
        """Whether a possible JSON opener remains hidden in the working tail."""
        return self._buf.rfind("{") > self._buf.rfind("}")

    def feed(self, delta: str) -> str:
        """Accumulate a text delta, returning the newly revealed visible text."""
        if not delta:
            return ""
        self._buf += delta
        visible = []

        def release(end):
            if end > self._released_len:
                text = self._buf[self._released_len : end]
                visible.append(text)
                self.text_parts.append(text)
                self._released_len = end

        while True:
            match = _CUSTOM_TOOL_OPEN_RE.search(self._buf, self._released_len)
            if match is None:
                # A trailing '{' could become an opener in the next delta.
                # Preserve that tail, but bound recovery for unmatched prose.
                last_open = self._buf.rfind("{", self._released_len)
                if last_open < 0:
                    release(len(self._buf))
                elif len(self._buf) - last_open > _GIVE_UP_BYTES:
                    release(len(self._buf) - _HOLD_BACK)
                else:
                    release(last_open)
                break
            release(match.start())
            try:
                opener_name = json.loads('"' + match.group(1) + '"')
            except (ValueError, json.JSONDecodeError):
                opener_name = ""
            if (
                opener_name
                and self._on_partial_name is not None
                and opener_name not in self._announced_names
            ):
                self._announced_names.add(opener_name)
                with contextlib.suppress(Exception):
                    self._on_partial_name(opener_name)
            end = _find_balanced_json_end(self._buf, match.start())
            if end is None:
                if len(self._buf) - match.start() > _GIVE_UP_BYTES:
                    release(match.start() + 1)
                    continue
                break
            obj = _safe_parse_tool_call_candidate(self._buf[match.start() : end])
            if (
                not isinstance(obj, dict)
                or not isinstance(obj.get("name"), str)
                or not obj["name"]
                or not isinstance(obj.get("arguments", {}), dict)
            ):
                # Advancing the search must also release the skipped brace.
                # Dropping it silently corrupted ordinary malformed JSON.
                release(match.start() + 1)
                continue
            self.completed.append(
                {
                    "id": f"call_custom_{len(self.completed) + 1}",
                    "type": "function",
                    "function": {
                        "name": obj["name"],
                        "arguments": json.dumps(
                            obj.get("arguments", {}), ensure_ascii=False
                        ),
                    },
                }
            )
            self._released_len = end
        self._buf = self._buf[self._released_len :]
        self._released_len = 0
        return "".join(visible)

    def drain(self) -> None:
        """Release any incomplete candidate when generation has ended."""
        if self._buf:
            self.text_parts.append(self._buf)
            self._buf = ""
        self._released_len = 0


def _find_balanced_json_end(text: str, start: int) -> int | None:
    """Find the index just past the closing brace of the JSON object that
    starts at ``text[start]``. Returns None if the braces don't balance
    (i.e. the stream hasn't delivered the closing brace yet).

    Counts ``{``/``}`` while correctly ignoring braces that appear inside
    JSON string literals (which can happen for things like ``"body": "{...}"``
    in a create_site body that contains CSS with braces).
    """
    depth = 0
    in_str = False
    escape = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return None


# Failure recovery for tool-call candidates whose ``body`` field
# contains unescaped ``"`` characters from HTML attribute syntax
# (e.g. ``target="_blank"``, ``href="..."``). Without the repair
# pass, ``json.loads`` raises ``JSONDecodeError`` because the
# balancer thinks the body string terminated early; the parser then
# ships the entire malformed JSON blob as raw visible text to the
# channel instead of executing the tool. See Z3ki's 2026-08-02
# "old-cartographers" create_site in #boing — the LLM emitted
# ~14 KB of partially-quoted HTML, the parser walked to EOF looking
# for a balanced close, the tool call never ran, and the user got a
# wall of broken text in 4 chunked Discord messages instead of a
# working site.


def _repair_unescaped_html_quotes(candidate: str) -> str | None:
    """Repair tool-call candidate JSON whose ``body`` field contains
    unescaped ``"`` characters from HTML attribute syntax (e.g.
    ``target="_blank"``, ``href="..."``).

    Returns the repaired candidate string, or ``None`` if no repair
    was applicable.

    Strategy:
      1. Locate the ``"body": "`` opener.
      2. Walk forward, tracking JSON escape state, until we hit an
         UNESCAPED ``"`` followed by ``}}`` — that's the body string
         terminator followed by the close of the ``arguments`` object
         and the close of the outer object. (LLMs that emit malformed
         HTML bodies almost always structure the close this way.)
      3. Re-encode the raw body slice with ``json.dumps`` (which
         properly escapes ``"`` and ``\\``), strip the outer quotes,
         and splice it back into the candidate.

    This is intentionally narrow — it only fires when a raw
    ``json.loads(candidate)`` already failed AND a ``"body": "`` field
    exists in the candidate. Clean JSON never reaches this path.
    """
    m = re.search(r'"body"\s*:\s*"', candidate)
    if not m:
        return None
    body_value_start = m.end()
    # Try each plausible terminator, cheapest-first, and keep the first
    # one that actually reparses into an object.
    for body_value_end in _body_terminator_candidates(candidate, body_value_start):
        body_escaped, repaired_any = _escape_body_slice(
            candidate, body_value_start, body_value_end
        )
        if not repaired_any:
            continue
        repaired = (
            candidate[:body_value_start] + body_escaped + candidate[body_value_end:]
        )
        try:
            obj, _end = json.JSONDecoder().raw_decode(repaired)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return repaired
    return None


def _body_terminator_candidates(candidate: str, body_value_start: int) -> list[int]:
    """Positions of every unescaped ``"`` in the body that could be the
    string's closing quote — i.e. one followed (modulo whitespace) by
    ``,`` or ``}``.

    The old code hardcoded a single terminator: an unescaped ``"``
    immediately followed by ``}}``. That only holds when ``body`` is the
    LAST key in ``arguments``. With ``{"body": "...", "title": "T"}`` the
    scan blew straight past the real terminator to the ``"}}`` at the end
    of the object, so ``title`` (and every other trailing key) was
    swallowed into the body string and silently lost.

    Quotes inside HTML attributes (``href="x"``) are followed by ``>``,
    ``/``, letters, etc. — never ``,`` or ``}`` — so they are not
    candidates. A body containing a literal ``",`` (e.g. ``said "hi",``)
    can still produce a false candidate, which is why the caller
    validates each one by reparsing and moves on if it does not hold.
    """
    out: list[int] = []
    i = body_value_start
    escape = False
    while i < len(candidate):
        ch = candidate[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < len(candidate) and candidate[j] in " \t\r\n":
                j += 1
            if j < len(candidate) and candidate[j] in ",}":
                out.append(i)
        i += 1
    return out


def _escape_body_slice(
    candidate: str, body_value_start: int, body_value_end: int
) -> tuple[str, bool]:
    r"""Re-escape the raw body slice. Returns ``(escaped, repaired_any)``.

    The body has a mix of already JSON-escaped sequences (``\\"``,
    ``\\\\``, ``\\n``) and bare ``"`` from HTML attributes that the LLM
    forgot to escape. Some bodies also contain bare newlines (the LLM
    emitted real newline chars instead of ``\\n`` escape sequences),
    which JSON forbids inside string literals. Walk the slice and:
      - preserve only sequences ``\\X`` where X is a real JSON escape
        char (``"``, ``\\``, ``/``, ``b``, ``f``, ``n``, ``r``, ``t``,
        ``u``) — these are the LLM's correct JSON escape attempts,
      - escape bare ``"``,
      - escape bare ``\\`` that is NOT followed by a JSON escape char
        (the LLM typo'd ``</div>`` as ``</div\\`` etc.),
      - escape bare control characters (literal newline, tab, CR).
    """
    body_chars: list[str] = []
    repaired_any = False
    i = body_value_start
    JSON_ESCAPE_CHARS = set('"\\/bfnrtu')
    while i < body_value_end:
        ch = candidate[i]
        if ch == "\\" and i + 1 < body_value_end:
            nxt = candidate[i + 1]
            if nxt in JSON_ESCAPE_CHARS:
                # Already-escaped JSON sequence; pass through as-is.
                body_chars.append(ch)
                body_chars.append(nxt)
                i += 2
                continue
            # Literal backslash not followed by a valid JSON escape char.
            # Escape it so the reparsed JSON keeps the backslash.
            body_chars.append("\\\\")
            repaired_any = True
            i += 1
            continue
        if ch == "\\":
            # Lone trailing backslash immediately before the terminator.
            # Left bare it would escape the closing quote and break the
            # reparse, so escape it too.
            body_chars.append("\\\\")
            repaired_any = True
            i += 1
            continue
        if ch == '"':
            body_chars.append('\\"')
            repaired_any = True
            i += 1
            continue
        if ch == "\n":
            body_chars.append("\\n")
            repaired_any = True
            i += 1
            continue
        if ch == "\r":
            body_chars.append("\\r")
            repaired_any = True
            i += 1
            continue
        if ch == "\t":
            body_chars.append("\\t")
            repaired_any = True
            i += 1
            continue
        body_chars.append(ch)
        i += 1
    return "".join(body_chars), repaired_any


def _safe_parse_tool_call_candidate(candidate: str):
    """Parse a candidate tool-call JSON, with one repair pass for the
    common failure mode of unescaped ``"`` characters in embedded HTML
    (``create_site`` body fields with ``target="_blank"``, ``href="..."``,
    etc).

    Returns the parsed dict on success, ``None`` if it cannot be parsed
    even after the repair attempt. Caller treats ``None`` as a
    false-positive opener and keeps searching.

    Three attempts:
      1. ``json.loads`` — clean JSON.
      2. ``json.JSONDecoder().raw_decode`` — tolerates trailing garbage
         (the LLM sometimes appends a hallucinated ``<parameter>`` tag
         after the JSON close, which we should ignore).
      3. ``_repair_unescaped_html_quotes`` + ``raw_decode`` — escapes
         unescaped ``"``, bare newlines, and bare backslashes inside
         a ``"body": "..."`` field, then parses.
    """
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        obj, _end = json.JSONDecoder().raw_decode(candidate)
        return obj
    except (json.JSONDecodeError, ValueError):
        pass
    repaired = _repair_unescaped_html_quotes(candidate)
    if repaired is None:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(repaired)
        return obj
    except (json.JSONDecodeError, ValueError):
        return None
