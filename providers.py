"""Ollama AI Provider for Maxwell Bot"""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# asyncio holds only a weak reference to a running task, so a bare
# `create_task(...)` whose result nobody keeps can be garbage-collected
# mid-flight and silently cancel the work. Keep a strong ref until it's done.
from utils import _spawn_background as _fire_and_forget  # noqa: E402


# Matches the `reasoning` string value inside a (possibly partial) tool-call
# arguments JSON. Models emit reasoning as the FIRST field, well before any
# huge field like create_site's `body`, so once this regex matches the value's
# closing quote is in hand and we can surface the reasoning to the live
# progress message without waiting for the rest of the stream.
_PARTIAL_REASONING_RE = re.compile(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"')


# ---------------------------------------------------------------------------
# Custom streaming tool-call protocol
# ---------------------------------------------------------------------------
#
# Native OpenAI-style tool_calls= doesn't stream incrementally on
# minimax-m3:cloud (or similar Ollama-cloud chat completion models): the
# entire {name, arguments} block arrives in ONE final delta at 88-100% of
# stream time, leaving the bot's "working on it…" progress message silent
# for the full 10-30s of generation.
#
# The "bare JSON on its own line" protocol sidesteps this: the model emits
# the tool call as part of the normal text stream (not the API's tool_calls
# field), and our SSE reader incrementally extracts it AS IT STREAMS. The
# model already knows raw JSON (no new syntax to learn) and the marker
# lands at ~12% of stream time vs ~88% for native — a real per-token
# progress signal for the user.
#
# Protocol shape (one JSON object on its own line, no fence, no tag):
#
#     {"name": "<tool>", "arguments": {<JSON object>}}
#
# The text around the JSON (the model's reply to the user) is preserved
# as normal assistant content. The JSON object is stripped from the
# visible reply so the user doesn't see raw JSON, but is captured into
# the ProviderResult.tool_calls so the rest of the dispatch flow treats
# it exactly like a native tool call.
#
# Streaming extraction (custom_tool_call_buffer below) does this:
#   1. Accumulates text deltas into a single buffer.
#   2. As soon as a `{"name": "..."` substring is visible, fires a
#      ``on_partial_name`` callback so the progress message can switch
#      from "thinking: …" to "<tool>: …" — even if the args haven't
#      finished streaming.
#   3. As soon as the outer JSON's closing brace is matched (counting
#      braces + tracking strings/escapes), parses it and returns a
#      native-format tool call list.
#   4. Continues looking for more tool calls (the model can chain
#      several in one response).
#
# The parser is conservative: if braces don't balance, the buffer is
# retained (we haven't hit the closing brace yet, just keep streaming).
# If JSON.parse fails on what we thought was complete, we rewind by one
# character and try again — handles the edge case where a brace inside
# a string fooled the counter.

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

# Conservative upper bound on the length of one opener match
# (e.g. `{"name": "<30-char tool name>"}`). If the unreleased tail is
# no longer than this, no future chunk can still split an opener
# across a boundary — release it all as visible. Keeping a separate
# constant from _HOLD_BACK (which is the "ambiguous" window for chunks
# still arriving) makes the intent obvious at the call site.
_CUSTOM_TOOL_OPEN_RE_MAX_LEN = 64


class _CustomToolCallBuffer:
    """Incrementally extracts bare-JSON tool calls from a streaming text delta.

    Constructed per-SSE-response. The SSE loop calls .feed(delta) for every
    text delta, then .drain() at the end to catch any final parse.

    For each tool call found:
      - ``on_partial_name(name)`` fires the moment ``{"name": "<tool>"`` is
        visible (mid-stream, even if args are still streaming) so the
        progress message can switch its UI prefix.
      - The completed tool call is appended to ``completed`` as a dict in
        the SAME shape as the provider's native tool_calls: ``{"id", "type",
        "function": {"name", "arguments"}}``. Callers can splice it
        straight into the existing dispatch flow.

    Anything in the stream that isn't a tool call JSON is preserved as
    ``text`` — the model's reply to the user, minus the JSON objects we
    stripped out.
    """

    def __init__(self, on_partial_name=None):
        self._buf = ""
        self.text_parts: list[str] = []
        self.completed: list[dict] = []
        self._on_partial_name = on_partial_name
        self._announced_names: set[str] = set()

    @property
    def has_pending_json(self) -> bool:
        """True when the buffer holds a bare-JSON tool call that's still
        being parsed (opener seen, close not yet). The progress UI can
        use this to show 'still writing…' instead of 'frozen' when the
        visible-content delta is empty for several frames.
        """
        return self._buf.rfind("{") > self._buf.rfind("}")

    def feed(self, delta: str) -> str:
        """Accumulate a new text delta. Extracts any complete bare-JSON
        tool calls and returns the newly-revealed VISIBLE text for this
        delta.

        Design: ``_buf`` is the running buffer. ``_released_len`` is the
        byte offset up to which text has been emitted as visible. On
        each feed:
          1. Append delta to _buf.
          2. Search _buf for the first opener past _released_len.
          3. If found, the text from _released_len to the opener is
             plain visible — emit it now and advance _released_len to
             the opener position. Then try to find a balanced end for
             the opener.
          4. If balanced end found, parse the candidate. Real tool
             call → append to completed, advance _released_len past
             the closer, and loop. Parse fail / wrong shape → advance
             past opener's first char (false-positive recovery) and
             loop.
          5. If no balanced end (opener mid-JSON): hold; the prefix
             already released covers everything safe. Any text after
             the opener (still buffering) is hidden.
          6. If no opener at all in the buffer: emit everything up to
             ``len(_buf) - _HOLD_BACK`` as visible. The trailing
             _HOLD_BACK window is held back so a chunk boundary can't
             split a fresh opener (max opener length is ~50 chars;
             _HOLD_BACK is comfortably larger).

        Works regardless of chunk size: we always search the full _buf
        from _released_len onward, so a tool call spanning 100 tiny
        deltas is found the moment the closing brace arrives. Text
        before the opener is released immediately, so the caller sees
        "All done!" the moment it streams in (not at drain time).
        """
        if not delta:
            return ""
        if not hasattr(self, "_released_len"):
            self._released_len = 0
        self._buf += delta
        newly_visible_total = ""
        while True:
            m = _CUSTOM_TOOL_OPEN_RE.search(self._buf, self._released_len)
            if not m:
                # No opener in the unreleased region. The only thing
                # that could be a "starter" for a future opener is a
                # bare `{` that's not yet followed by enough text. Find
                # the last `{` in the unreleased region and hold back
                # from there — anything before that `{` cannot grow
                # into an opener, so it's safe to release as visible.
                # If there's no `{` at all, release the whole thing.
                unreleased = self._buf[self._released_len :]
                last_open = unreleased.rfind("{")
                if last_open == -1:
                    # No possible opener prefix. Release all.
                    release_to = len(self._buf)
                else:
                    # Hold back from the last `{` onward; release
                    # everything before it as visible.
                    release_to = self._released_len + last_open
                if release_to > self._released_len:
                    nv = self._buf[self._released_len : release_to]
                    self.text_parts.append(nv)
                    self._released_len = release_to
                    newly_visible_total += nv
                break
            # Opener found. Text BEFORE the opener is plain visible.
            if m.start() > self._released_len:
                prefix = self._buf[self._released_len : m.start()]
                self.text_parts.append(prefix)
                self._released_len = m.start()
                newly_visible_total += prefix
            # Fire partial-name callback (idempotent).
            opener_name = m.group(1)
            if (
                self._on_partial_name is not None
                and opener_name not in self._announced_names
            ):
                self._announced_names.add(opener_name)
                with contextlib.suppress(Exception):
                    self._on_partial_name(str(opener_name))
            # Try to find a balanced end for this opener.
            end = _find_balanced_json_end(self._buf, m.start())
            if end is None:
                # Opener mid-JSON. Hold unless the held region is huge (unescaped
                # quotes in create_site HTML, CSS `{`, etc.) — then skip the
                # false opener so a later valid tool call can still parse.
                if len(self._buf) - m.start() > _GIVE_UP_BYTES:
                    self._released_len = m.start() + 1
                    continue
                break
            # Validate by parsing. Must go through
            # _safe_parse_tool_call_candidate, NOT bare json.loads: that
            # is where the unescaped-HTML-quote repair lives. With plain
            # json.loads here, a create_site whose body contains
            # `href="..."` failed to parse, the opener was skipped as a
            # "false positive", and the whole malformed blob shipped to
            # the channel as raw visible text while the tool never ran —
            # the exact 2026-08-02 incident the repair pass was written
            # for. The repair was only ever reachable from a dead code
            # path, so the live streaming path never benefited from it.
            candidate = self._buf[m.start() : end]
            obj = _safe_parse_tool_call_candidate(candidate)
            if obj is None:
                # Balanced but not valid JSON even after repair — false
                # positive. Skip past the opener's first char and keep
                # searching.
                self._released_len = m.start() + 1
                continue
            if not isinstance(obj, dict) or not obj.get("name"):
                # Not a tool-call shape. Advance past the opener.
                self._released_len = m.start() + 1
                continue
            # Real tool call. Append to completed and advance past
            # the closer; loop continues for any further text/calls.
            tool_name = str(obj.get("name", ""))
            args = obj.get("arguments", {})
            if not isinstance(args, dict):
                args = {}
            self.completed.append(
                {
                    "id": f"call_custom_{len(self.completed) + 1}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
            self._released_len = end
        return newly_visible_total

    def drain(self) -> None:
        """Final call after the stream ends. Emit any remaining unreleased
        text as visible. If there's a partial tool call still in flight
        (opener received but no closing brace), it can't have been a real
        tool call — the stream is over — so emit the held opener region as
        visible text too.
        """
        if not hasattr(self, "_released_len"):
            return
        held = self._buf[self._released_len :]
        if held:
            self.text_parts.append(held)
            self._released_len = len(self._buf)
        # If _buf grew unreasonably large pointing at a never-closed
        # opener, that opener was a false positive (e.g. it appeared
        # mid-string); drop the held region so we don't carry junk.
        # (We only get here after the loop above stopped finding a
        # balanced end for the opener.)


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


async def _safe_call(cb, *args, **kwargs):
    """Await an SSE callback, swallowing any exception. Used for fire-and-forget
    callbacks (``_fire_and_forget(_safe_call(...))``) so a buggy callback
    never crashes the streaming read loop."""
    try:
        await cb(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.debug("SSE callback raised: %s", e)


def _append_tool_call_arguments(slot: dict, incoming) -> None:
    """Accumulate streaming tool-call arguments onto ``slot``.

    OpenAI streams ``function.arguments`` as JSON *strings* that must be
    concatenated. Some OpenAI-compatible providers (GLM-5.x on OpenCode
    Zen Go) send a finished object in one delta instead — concatenating
    that with ``""`` raises TypeError and kills the turn.
    """
    fn = slot.setdefault("function", {})
    existing = fn.get("arguments") or ""
    if isinstance(existing, dict):
        existing = json.dumps(existing, ensure_ascii=False)
    if isinstance(incoming, dict):
        fn["arguments"] = json.dumps(incoming, ensure_ascii=False)
        return
    if incoming is None:
        fn["arguments"] = existing
        return
    fn["arguments"] = existing + (
        incoming if isinstance(incoming, str) else str(incoming)
    )


def _extract_partial_reasoning(arguments: str) -> str:
    """Best-effort pull of the `reasoning` string from a PARTIAL arguments JSON.

    Returns '' until the reasoning value's closing quote has arrived (i.e. the
    model is still emitting it). Once complete, returns the decoded string.
    Used to update the in-channel progress message with the model's real intent
    mid-stream, instead of a static "generating…" for the whole generation.
    """
    if not arguments:
        return ""
    if isinstance(arguments, dict):
        r = arguments.get("reasoning")
        return r if isinstance(r, str) else ""
    if not isinstance(arguments, str):
        arguments = str(arguments)
    # Fast path: the whole arguments object already parses.
    try:
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            r = parsed.get("reasoning")
            if isinstance(r, str):
                return r
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # Partial JSON: grab the reasoning value once its closing quote landed.
    m = _PARTIAL_REASONING_RE.search(arguments)
    if not m:
        return ""
    raw = m.group(1)
    try:
        return json.loads('"' + raw + '"')  # decode \n, \", etc.
    except (json.JSONDecodeError, ValueError):
        return raw


async def _read_sse_response(
    resp: aiohttp.ClientResponse,
    on_tool_call_name=None,
    on_token=None,
    custom_tool_calls: bool = False,
) -> dict:
    """Read an OpenAI-style SSE chat-completions stream and reassemble it into
    the same dict shape a non-streamed `await resp.json()` would return.

    If ``on_tool_call_name`` is provided, it's awaited the first time a
    tool_call delta arrives with a function name. This lets the caller
    update a live progress message mid-stream — e.g. show
    "create_site: …" while the model is still generating the tool arguments
    (the HTML body), instead of waiting for the entire response to finish.

    If ``on_token`` is provided, it's called (fire-and-forget, NEVER awaited
    inline) on every content and reasoning delta so the caller can show a
    live progress message with a rolling preview of the model's own words.
    Inline awaiting would back-pressure the SSE read on a slow Discord edit
    and stall the upstream. The callback gets a small dict with the new
    delta (NOT an accumulator) plus a flag distinguishing reasoning from
    visible content::

        {"reasoning": str, "content": str, "tool_name": str|None}

    ``tool_name`` is set only on the delta that first introduces a tool call
    name (so the callback can switch the progress UI from "model is
    thinking" to "tool_name: …" the moment the model decides).

    The OpenAI streaming protocol sends one JSON object per ``data:`` line, each
    with the same frame structure but only the *delta* of what changed since
    the previous frame:

        data: {"choices": [{"delta": {"role": "assistant"}, "index": 0}]}
        data: {"choices": [{"delta": {"content": "hello"}, "index": 0}]}
        data: {"choices": [{"delta": {"content": " world"}, "index": 0}]}
        data: {"choices": [{"delta": {"tool_calls": [...]}, "index": 0}]}
        data: {"choices": [{"finish_reason": "stop", "index": 0}]}
        data: [DONE]

    We accumulate content strings, tool_calls (pinned by ``index``), and any
    usage payload that streams in at the end, then return a dict that matches
    the non-streamed response shape so the rest of the request handler does
    not need to care which mode produced the response.

    Returns the merged dict, plus (via a sentinel) the time the first content
    delta was received — encoded as ``__first_token_ms__`` in the returned
    dict and popped by the caller.

    Raises RuntimeError if the stream is malformed (no choices ever arrive) so
    the upstream retry logic can take over.
    """
    merged: dict = {"choices": [{}]}
    tool_calls_by_index: dict[int, dict] = {}
    content_parts: list[str] = []
    role: str | None = None
    finish_reason: str | None = None
    reasoning_parts: list[str] = []
    first_token_s: float | None = None
    done = False
    # When custom_tool_calls=True, we route text deltas through this buffer
    # which incrementally extracts bare-JSON tool calls ({"name": "...",
    # "arguments": {...}}) and synthesizes native-format tool_calls. This
    # is the workaround for providers (Ollama cloud's minimax-m3) that
    # bundle the entire tool_call into one final delta and never stream
    # it incrementally. With this on, the tool name lands in the
    # progress UI at ~12% of stream time vs ~88% with native tools=.
    # See _CustomToolCallBuffer for the protocol details.
    custom_buffer: _CustomToolCallBuffer | None = (
        _CustomToolCallBuffer(
            on_partial_name=lambda nm: (
                # 2026-07-21: fire BOTH callbacks when the JSON
                # opener is seen mid-stream. The old code only fired
                # on_token (so the progress UI could switch its
                # 'thinking:' → 'using <tool>…' transition) but
                # skipped on_tool_call_name. That meant the bot's
                # _on_tool_call_name callback (which sets
                # _current_tool on the progress and triggers
                # progress.update() with the tool's reasoning once
                # run_one() dispatches) was never invoked — and the
                # progress buffer kept the raw streaming JSON
                # content instead of the natural-language reasoning
                # the model wrote. Now both fire on opener, so the
                # progress UI immediately shows the tool name AND
                # the subsequent update() replaces the buffer with
                # the actual reasoning sentence.
                on_token({"content": "", "reasoning": "", "tool_name": nm})
                if on_token is not None
                else None
            )
        )
        if custom_tool_calls
        else None
    )

    # Bridge: the custom protocol's on_partial_name callback can't
    # directly invoke the bot's async on_tool_call_name (it's sync
    # from inside the brace-balancer). Wire it through a fire-and-
    # forget task so the bot's _on_tool_call_name fires as soon as
    # the tool name is parsed, not only when run_one() reaches it.
    if custom_tool_calls and on_tool_call_name is not None:
        # Patch the on_partial_name to also schedule on_tool_call_name
        original = custom_buffer._on_partial_name

        def _bridge(nm, _orig=original, _cb=on_tool_call_name):
            if _orig is not None:
                _orig(nm)
            try:
                _fire_and_forget(_safe_call(_cb, nm, ""))
            except RuntimeError:
                pass

        custom_buffer._on_partial_name = _bridge

    buf = b""
    async for raw_chunk in resp.content.iter_any():
        if done:
            break
        buf += raw_chunk
        while b"\n" in buf and not done:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            # SSE comments / non-data lines start with ":" — ignore.
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].lstrip()
            if payload == b"[DONE]":
                done = True
                break
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                # Malformed frame — skip rather than fail the whole stream.
                # Providers occasionally send keepalives or partial frames.
                continue
            if first_token_s is None:
                first_token_s = time.perf_counter()
            for choice in obj.get("choices", []) or []:
                idx = choice.get("index", 0)
                # Ensure the choices slot for this index exists.
                while len(merged["choices"]) <= idx:
                    merged["choices"].append({})
                delta = choice.get("delta") or {}
                if delta.get("role"):
                    role = delta["role"]
                visible_content_delta = ""
                if "content" in delta and delta["content"] is not None:
                    content_parts.append(delta["content"])
                    # Custom tool-call protocol: pipe text deltas through
                    # the extractor so tool calls embedded as bare JSON
                    # in the text stream get parsed incrementally and
                    # stripped from the visible content. Native path:
                    # leave content_parts alone.
                    #
                    # feed() returns the VISIBLE portion of this delta
                    # (JSON already stripped, or "" while a tool-call
                    # opener is still balancing). That return value — not
                    # the raw delta — is what the on_token progress
                    # preview below must use. Using the raw delta here
                    # used to leak the model's literal bare-JSON tool
                    # call (e.g. '{"name": "shell", "arguments": {...')
                    # into the "thinking: …" status line character by
                    # character, since native tool_name detection for the
                    # custom protocol only fires once the opener is fully
                    # parsed, not as raw text streams in.
                    if custom_buffer is not None:
                        visible_content_delta = custom_buffer.feed(delta["content"])
                    else:
                        visible_content_delta = delta["content"]
                # Reasoning deltas: OpenAI/DeepSeek-style models use
                # `reasoning_content`; Ollama cloud's minimax-m3 emits a
                # `reasoning` field on the same delta. Treat both the same
                # way so the bot's existing reasoning handler picks them up.
                for rkey in ("reasoning_content", "reasoning"):
                    rval = delta.get(rkey)
                    if rval is not None:
                        reasoning_parts.append(rval)
                # Per-token progress callback (fire-and-forget, NEVER awaited
                # inline). A slow Discord edit must not back-pressure the SSE
                # read — that would stall the upstream provider and add visible
                # latency to the stream. We hand the caller a small dict with
                # the NEW deltas from this frame plus an empty tool_name that
                # the tool_call block below may fill in.
                #
                # 2026-07-21: in the custom tool-call protocol, the model
                # often emits the entire reasoning + tool call as a single
                # huge JSON object — so the visible-content delta is empty
                # for most frames and the progress UI just sits on
                # "working on it…". Pass a short HEAD of the raw content
                # as a "still streaming" preview so the user sees the
                # model is alive and writing. The bot's tick() rate-limits
                # this anyway (3s between edits), so the volume is
                # harmless.
                if on_token is not None:
                    tok_content = visible_content_delta
                    tok_reason = ""
                    for rkey in ("reasoning_content", "reasoning"):
                        rv = delta.get(rkey)
                        if rv:
                            tok_reason = rv
                            break
                    # 2026-07-21: when the custom buffer is mid-JSON
                    # (model is emitting a bare-JSON tool call), DON'T
                    # surface the raw content as a progress preview.
                    # The raw text is JSON like 'name create_site ,
                    # arguments reason ing ...' which fills the
                    # progress buffer with unreadable fragments. The
                    # bot's _on_tool_call_name callback (bridged from
                    # on_partial_name) sets the tool name so the line
                    # shows 'using <tool>…' until run_one() lands with
                    # the actual natural-language reasoning via
                    # progress.update(name, tool_reasoning).
                    if (
                        not tok_content
                        and not tok_reason
                        and custom_buffer is not None
                        and custom_buffer.has_pending_json
                    ):
                        # Skip the on_token callback only — do NOT continue the
                        # choice loop or we drop native tool_calls on this delta.
                        pass
                    elif tok_content or tok_reason:
                        try:
                            on_token(
                                {
                                    "content": tok_content,
                                    "reasoning": tok_reason,
                                    "tool_name": None,
                                }
                            )
                        except Exception:
                            pass
                if "tool_calls" in delta and delta["tool_calls"]:
                    for tc_delta in delta["tool_calls"]:
                        tc_idx = tc_delta.get("index", 0)
                        slot = tool_calls_by_index.get(tc_idx)
                        if slot is None:
                            slot = {
                                "id": tc_delta.get("id"),
                                "type": tc_delta.get("type", "function"),
                                "function": {"name": "", "arguments": ""},
                            }
                            tool_calls_by_index[tc_idx] = slot
                        if tc_delta.get("id"):
                            slot["id"] = tc_delta["id"]
                        if tc_delta.get("type"):
                            slot["type"] = tc_delta["type"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = (
                                slot["function"].get("name", "") + fn["name"]
                            )
                            # Fire the tool-name callback the first time we
                            # see it. This is the *old* path kept for
                            # backwards-compat (legacy callers still use it).
                            # The new ``on_token`` path below also surfaces
                            # the tool name to the per-token progress callback
                            # so the UI can switch from "model is thinking"
                            # to "<tool_name>: …" the moment the model
                            # commits to a tool.
                            if on_tool_call_name is not None and not slot.get(
                                "_name_sent"
                            ):
                                slot["_name_sent"] = True
                                cb = on_tool_call_name
                                args = (slot["function"]["name"], "")
                                try:
                                    _fire_and_forget(_safe_call(cb, *args))
                                except RuntimeError:
                                    with contextlib.suppress(Exception):
                                        await cb(*args)
                            # Same signal on the new per-token path. The
                            # token callback is fire-and-forget so a slow
                            # Discord edit doesn't stall the SSE read.
                            if on_token is not None and not slot.get(
                                "_token_name_sent"
                            ):
                                slot["_token_name_sent"] = True
                                try:
                                    on_token(
                                        {
                                            "content": "",
                                            "reasoning": "",
                                            "tool_name": slot["function"]["name"],
                                        }
                                    )
                                except Exception:
                                    pass
                        if fn.get("arguments"):
                            _append_tool_call_arguments(slot, fn["arguments"])
                            # Surface the model's reasoning mid-stream so the
                            # progress message shows intent (not a static
                            # "generating…") during long argument generation
                            # (e.g. create_site's HTML body). Reasoning is
                            # usually the first field emitted, so it completes
                            # well before the big fields. Fires once per call.
                            if on_tool_call_name is not None and not slot.get(
                                "_reasoning_sent"
                            ):
                                reason = _extract_partial_reasoning(
                                    slot["function"]["arguments"]
                                )
                                if reason:
                                    slot["_reasoning_sent"] = True
                                    cb = on_tool_call_name
                                    args = (slot["function"]["name"], reason)
                                    try:
                                        _fire_and_forget(_safe_call(cb, *args))
                                    except RuntimeError:
                                        with contextlib.suppress(Exception):
                                            await cb(*args)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            # Some providers stream usage in the final frame (Anthropic-style
            # models on OpenRouter do this; OpenAI does it when
            # stream_options.include_usage=true).
            if obj.get("usage"):
                merged["usage"] = obj["usage"]
        else:
            # No inner break — keep iterating. Outer loop continues.
            continue
        # Inner break hit [DONE]; stop reading.
        break

    if (
        not tool_calls_by_index
        and not content_parts
        and not role
        and finish_reason is None
        and (custom_buffer is None or not custom_buffer.completed)
    ):
        raise RuntimeError("Provider stream produced no choices")

    # Custom tool-call protocol: drain any final tail and merge results.
    # The extracted tool calls use the same native shape (id, type, function)
    # so the rest of the dispatch path treats them identically to native
    # tool_calls=. The visible content has any bare-JSON tool calls already
    # stripped out (the model wrote them as a single line; the user sees
    # the surrounding reply without the raw JSON).
    if custom_buffer is not None:
        custom_buffer.drain()
        if custom_buffer.completed:
            # Append custom-extracted calls to any native ones. Native
            # tool_calls (if any) are already accumulated; this just
            # adds the bare-JSON ones we parsed out of the text.
            for tc in custom_buffer.completed:
                tool_calls_by_index[len(tool_calls_by_index)] = tc
            # Rebuild visible content from the buffer's text_parts (with
            # JSON objects stripped), overriding the raw content_parts
            # we accumulated.
            # Always rebuild from the buffer, including when text_parts is
            # empty (JSON-only tool turn). Gating on truthiness left the raw
            # JSON in content_parts for the instructed "JSON line first" shape.
            content_parts = ["".join(custom_buffer.text_parts)]

    # Sort tool calls by their index so the order matches the model's intent.
    # Strip the internal callback-tracking flags ("_name_sent"/"_reasoning_sent")
    # so they never leak into the tool_calls we hand back to the provider.
    tool_calls_list = [
        {
            k: v
            for k, v in tool_calls_by_index[idx].items()
            if not str(k).startswith("_")
        }
        for idx in sorted(tool_calls_by_index)
    ]
    message: dict = {"role": role or "assistant"}
    if content_parts:
        message["content"] = "".join(content_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls_list:
        message["tool_calls"] = tool_calls_list

    # The first (and typically only) choice carries the finished message.
    merged["choices"][0] = {
        "index": 0,
        "message": message,
        "finish_reason": finish_reason,
    }
    merged["__first_token_s__"] = first_token_s
    return merged


# When an endpoint returns a 429 (rate-limited / usage-exhausted), we temporarily
# steer traffic away from it for this long instead of retrying it in the same
# request. This avoids hammering a shared upstream pool (e.g. OpenRouter's
# pooled free keys) that is already rate-limiting us, which only makes the
# limit worse. Override via OLLAMA_ENDPOINT_COOLDOWN_SECONDS.
DEFAULT_ENDPOINT_COOLDOWN_SECONDS = 60.0

USAGE_EXHAUSTED_MESSAGE = (
    "The api is down cuz yall drained the usage and im not rich so wait like 2 hours"
)

AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}

MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


class ProviderUsageExhaustedError(RuntimeError):
    """Raised when the upstream provider is out of quota, credits, or cooldown capacity."""

    user_message = USAGE_EXHAUSTED_MESSAGE


class ProviderRequestError(RuntimeError):
    """A deterministic non-2xx that every available endpoint already rejected.

    Retrying with the same payload reproduces it exactly, so the retry loop
    re-raises this instead of sleeping through its remaining attempts.
    """


class ProviderResult(str):
    """A ``str`` subclass carrying per-call ``tool_calls`` / ``usage``.

    Behaves exactly like a ``str`` everywhere a string is expected (f-strings,
    ``len()``, ``or ""``, ``str()``, slicing, etc.), but also exposes the
    native tool calls and token usage for *this specific call* so the caller
    does not have to read shared provider instance state.

    Reading ``provider._last_tool_calls`` / ``provider._last_usage`` after an
    ``await`` was racy: with ``ai_concurrency > 1`` (or background ticks sharing
    the same provider), a concurrent ``generate_response`` could overwrite the
    shared state between the call and the consume, causing one channel to
    execute another channel's tool calls. Attaching the values to the returned
    object makes the handoff per-call and race-free.
    """

    __slots__ = ("tool_calls", "usage", "assistant_message")

    def __new__(
        cls,
        content,
        tool_calls: list | None = None,
        usage: dict | None = None,
        assistant_message: dict | None = None,
    ):
        inst = super().__new__(
            cls, content if isinstance(content, str) else str(content or "")
        )
        inst.tool_calls = list(tool_calls) if tool_calls else []
        inst.usage = dict(usage) if usage else {}
        inst.assistant_message = assistant_message
        return inst


def _is_usage_exhausted_error(status: int, error_text: str) -> bool:
    """Detect true quota/credit exhaustion — not ordinary rate limits.

    Transient 429 rate limits must still get normal retry/backoff. Only treat as
    exhausted when the body clearly indicates cooldown, quota, or credits.
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
        "out of credits",
        "out of quota",
        "billing hard limit",
        "spend limit",
    )
    if status != 429:
        return False
    # Ordinary rate limiting is NOT exhausted. Only flag as exhausted when
    # the body also clearly mentions quota/credit markers; rate-limit alone
    # means transient and the caller should keep retrying.
    is_rate_limit = (
        "rate limit" in text or "rate_limit" in text or "too many requests" in text
    )
    is_quota_marker = any(m in text for m in markers)
    if is_rate_limit and not is_quota_marker:
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


def _strip_media_parts(chat_messages: list[dict]) -> bool:
    """Flatten multimodal content back to plain text. True if anything changed.

    Last resort when every endpoint rejects the attachments: sending the text
    alone beats dropping the user's message on the floor.
    """
    changed = False
    for msg in chat_messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        dropped = len(content) - len(texts)
        merged = "\n".join(t for t in texts if t).strip()
        if dropped > 0:
            merged = (
                f"{merged}\n[{dropped} attachment(s) omitted — "
                "no available model could accept them]"
            ).strip()
        msg["content"] = merged
        changed = True
    return changed


@dataclass(frozen=True)
class ProviderEndpoint:
    name: str
    base_url: str
    model: str
    api_key: str = ""
    disable_reasoning: bool = False


def normalize_base_url(base_url: str) -> str:
    """Normalize an OpenAI-compatible base URL to the API root.

    Requests are built as ``{base_url}/chat/completions``, so the base has
    to include the API path segment. Everyone pastes the bare host
    ("http://localhost:11434", "https://api.openai.com"), which then 404s in
    a way that looks like a broken bot rather than a missing "/v1". If the
    URL carries no path at all we add the conventional one; a URL that
    already has a path (/v1, /v2, /api/v1, ...) is left exactly as given.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return base
    _, _, rest = base.partition("://")
    if not rest:  # no scheme: treat the whole thing as a host
        rest = base
    if "/" in rest:  # already carries a path — the operator's business
        return base
    return f"{base}/v1"


class OllamaProvider:
    """OpenAI-compatible LLM Provider with multimodal support using /v1/chat/completions"""

    def __init__(
        self,
        base_url: str,
        model: str,
        max_tokens: int,
        temperature: float,
        api_key: str = "",
        disable_reasoning: bool = True,
        fallback_base_url: str = "",
        fallback_model: str = "",
        fallback_api_key: str = "",
        fallback_disable_reasoning: bool = True,
        retry_attempts: int = 3,
        enable_audio_input: bool = False,
        vision_base_url: str = "",
        vision_model: str = "",
        vision_api_key: str = "",
        vision_disable_reasoning: bool = True,
    ):
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = api_key.strip()
        self.retry_attempts = max(1, retry_attempts)
        self.enable_audio_input = bool(enable_audio_input)
        self._endpoints = [
            ProviderEndpoint(
                "primary", self.base_url, self.model, self.api_key, disable_reasoning
            ),
        ]
        if fallback_base_url and fallback_model:
            self._endpoints.append(
                ProviderEndpoint(
                    "fallback",
                    normalize_base_url(fallback_base_url),
                    fallback_model,
                    fallback_api_key.strip(),
                    fallback_disable_reasoning,
                )
            )
        # Appended last so text routing can keep treating index 1 as fallback.
        vision_model = (vision_model or "").strip()
        if vision_model:
            self._endpoints.append(
                ProviderEndpoint(
                    "vision",
                    normalize_base_url(vision_base_url or self.base_url),
                    vision_model,
                    (vision_api_key or self.api_key).strip(),
                    vision_disable_reasoning,
                )
            )
        self._session = None
        self.available = False
        self._last_usage: dict = {}
        self._last_tool_calls: list = []
        self._last_assistant_message: dict | None = None
        # Per-endpoint learned max *output* token cap (name -> cap). Set when a
        # 400 "maximum output tokens" is observed, and applied proactively on
        # the next call to that endpoint so we don't waste a round-trip on the
        # 400 again. Scoped per-endpoint (NOT on the shared instance) so one
        # model's small output cap doesn't cripple other endpoints/concurrent
        # requests that previously got mutated via self.max_tokens.
        self._endpoint_output_caps: dict[str, int] = {}
        # Same idea for models that accept exactly one temperature (Console Go
        # rejects anything but 0.6 with a 400). Learned once, applied up front.
        self._endpoint_temperatures: dict[str, float] = {}
        # Endpoints that have proven they cannot accept attachments (e.g. a
        # text-only fallback like inclusionai/ling-3.0-flash 404ing with "No
        # endpoints found that support image input"). Remembered across calls
        # so every subsequent image turn skips them instead of re-paying for
        # the same round-trip. Whether an endpoint's model is multimodal does
        # not change between requests, so this never needs to expire.
        self._media_incapable: set[str] = set()
        # Per-endpoint rate-limit cooldown: name -> monotonic expiry. While an
        # endpoint is cooling, _attempt_endpoint steers to an alternative (if
        # any) so a rate-limited upstream isn't retried immediately.
        self._endpoint_cooldown: dict[str, float] = {}
        try:
            self._cooldown_seconds = float(
                os.getenv(
                    "OLLAMA_ENDPOINT_COOLDOWN_SECONDS",
                    str(DEFAULT_ENDPOINT_COOLDOWN_SECONDS),
                )
                or DEFAULT_ENDPOINT_COOLDOWN_SECONDS
            )
        except (TypeError, ValueError):
            self._cooldown_seconds = DEFAULT_ENDPOINT_COOLDOWN_SECONDS

    def _headers(self, endpoint: ProviderEndpoint = None) -> dict[str, str]:
        api_key = self.api_key if endpoint is None else endpoint.api_key
        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    def _endpoint_named(self, name: str) -> ProviderEndpoint | None:
        for ep in self._endpoints:
            if ep.name == name:
                return ep
        return None

    def _reasoning_content_is_answer(
        self,
        endpoint: ProviderEndpoint | None,
        message: dict,
    ) -> bool:
        """Return True only when this provider's reasoning_content holds a real
        user-facing answer rather than internal chain-of-thought.

        A null `content` + non-empty `reasoning_content` is ambiguous: some
        models (DeepSeek-family) put the actual answer in reasoning_content;
        an interrupted/cut-off reasoning model (grok, ollama-native,
        minimax-m3) leaves only scratchpad there with no answer at all.
        Promoting scratchpad to content is what leaked the bot's reasoning to
        Discord. We only trust reasoning_content as an answer for the models
        that are known to ship text that way; for everything else we let the
        empty-response retry/fallback take over.
        """
        model = (endpoint.model if endpoint is not None else self.model) or ""
        m = model.lower()
        # DeepSeek-family convention: answer may ride in reasoning_content.
        return any(tok in m for tok in ("deepseek", "deep_seek", "deepseek-r1"))

    def _media_endpoint_order(self) -> list[ProviderEndpoint]:
        """Vision first, then the rest. Text-only primaries 400 on image_url."""
        vision = self._endpoint_named("vision")
        ordered: list[ProviderEndpoint] = []
        if vision is not None:
            ordered.append(vision)
        for ep in self._endpoints:
            if ep not in ordered:
                ordered.append(ep)
        # Endpoints already known to reject attachments go last rather than
        # being dropped: if they're all we have left, a doomed try still beats
        # refusing to send anything.
        return sorted(ordered, key=lambda ep: ep.name in self._media_incapable)

    def _attempt_endpoint(
        self,
        attempt: int,
        *,
        fast_fallback: bool = False,
        has_media: bool = False,
    ) -> ProviderEndpoint:
        primary = self._endpoint_named("primary") or self._endpoints[0]
        fallback = self._endpoint_named("fallback")
        vision = self._endpoint_named("vision")

        if has_media and vision is not None:
            # A fallback that has already proven text-only is not a media
            # option; sending it an image_url just buys another 404.
            if fallback is not None and fallback.name in self._media_incapable:
                fallback = None
            if fast_fallback:
                natural = vision if attempt == 1 else (fallback or vision)
            else:
                # Attempts 1-2: vision model; 3+: text fallback if configured.
                natural = vision if attempt <= 2 else (fallback or vision)
            if self._is_endpoint_cooling(natural.name):
                for ep in (vision, fallback, primary):
                    if ep is not None and not self._is_endpoint_cooling(ep.name):
                        return ep
            return natural

        if fallback is None:
            return primary
        if fast_fallback:
            natural = primary if attempt == 1 else fallback
        else:
            # Attempt 1 and 2: primary (main)
            # Attempt 3 and beyond: fallback (second provider)
            natural = primary if attempt <= 2 else fallback
        # If the chosen endpoint is rate-limit cooling and a healthy alternative
        # exists, skip straight to it. This turns a 429 on a shared upstream into
        # an immediate fallback instead of a doomed same-endpoint retry.
        # Skip the vision endpoint on text turns — it is reserved for media.
        if self._is_endpoint_cooling(natural.name):
            for ep in self._endpoints:
                if ep.name == "vision":
                    continue
                if not self._is_endpoint_cooling(ep.name):
                    return ep
        return natural

    def _is_endpoint_cooling(self, name: str) -> bool:
        expiry = self._endpoint_cooldown.get(name)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            self._endpoint_cooldown.pop(name, None)
            return False
        return True

    def _cool_endpoint(self, name: str, reason: str = "rate-limited") -> None:
        self._endpoint_cooldown[name] = time.monotonic() + self._cooldown_seconds
        logger.warning(
            "Provider endpoint %s %s; cooling for %.0fs (using alternative if available)",
            name,
            reason,
            self._cooldown_seconds,
        )

    def _should_wait_before_retry(
        self, current: ProviderEndpoint, next_endpoint: ProviderEndpoint
    ) -> bool:
        return current.name == next_endpoint.name

    def _request_payload(
        self,
        endpoint: ProviderEndpoint,
        chat_messages: list[dict],
        tools: list[dict] = None,
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        disable_reasoning: bool = None,
    ) -> dict:
        # Model override is honored ONLY on the primary endpoint. Fallback
        # endpoints keep their configured model because the fallback is
        # selected precisely because the primary model is unhealthy. If a
        # caller passed a model override but we're routing to a fallback,
        # log a debug line so it's visible why their model was swapped.
        if model and endpoint.name != "primary" and model != endpoint.model:
            logger.debug(
                "Model override %r ignored on fallback endpoint %r (using %r)",
                model,
                endpoint.name,
                endpoint.model,
            )
        effective_temperature = self.temperature if temperature is None else temperature
        # An endpoint that already rejected our temperature gets its demanded
        # value up front instead of another guaranteed 400.
        forced_temperature = self._endpoint_temperatures.get(endpoint.name)
        if forced_temperature is not None:
            effective_temperature = forced_temperature
        data = {
            "model": (model or endpoint.model)
            if endpoint.name == "primary"
            else endpoint.model,
            "messages": chat_messages,
            "temperature": effective_temperature,
            "stream": True,
        }
        # Always include max_tokens from config or override
        effective_max = max_tokens if max_tokens is not None else self.max_tokens
        # Proactively clamp to a previously-learned per-endpoint output cap so
        # we don't waste a round-trip re-hitting the same 400. Per-endpoint so a
        # small-cap model never lowers the cap for other endpoints.
        learned_cap = self._endpoint_output_caps.get(endpoint.name)
        if learned_cap and effective_max > learned_cap:
            effective_max = learned_cap
        data["max_tokens"] = effective_max
        # Per-call disable_reasoning overrides the endpoint default; a caller
        # that passes disable_reasoning=False can keep reasoning on a shared
        # provider whose endpoint.disable_reasoning is True.
        use_disable_reasoning = (
            disable_reasoning
            if disable_reasoning is not None
            else endpoint.disable_reasoning
        )
        if use_disable_reasoning:
            # Ollama's OpenAI-compatible endpoint accepts both shapes from its
            # /v1/chat/completions docs: top-level `reasoning_effort: "none"`
            # OR nested `reasoning: {"effort": "none"}`. The literal string
            # "none" is what the docs list as a valid value (alongside "low",
            # "medium", "high", "max") — sending boolean false or
            # {"exclude": true} (OpenRouter-style) was a no-op against Ollama,
            # which is why reasoning kept streaming even with
            # disable_reasoning=True. Emit both shapes so the same payload
            # works across Ollama and OpenRouter without branching.
            data["reasoning_effort"] = "none"
            data["reasoning"] = {"effort": "none"}
            data["thinking"] = {"type": "disabled", "budget_tokens": 0}
        elif "kimi-k2.7" in str(data.get("model") or "").lower():
            # OpenCode Go's kimi-k2.7-code rejects reasoning_effort=none
            # ("invalid thinking: only type=enabled is allowed") and, if we
            # omit the thinking field, streams reasoning until max_tokens
            # with an empty content delta. Pin thinking on so the visible
            # reply actually arrives.
            data["thinking"] = {"type": "enabled"}
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
        return data

    async def _get_session(self):
        if self._session is None or self._session.closed:
            # BUG FIX: do NOT use SSRF-safe resolver for the provider session.
            # The default provider URL is localhost:11434 (local Ollama), and
            # the safe resolver blocks all private/loopback addresses.
            # The provider is operator-configured via env vars, not user input.
            # SSRF protection belongs on the shared session used by tools like
            # fetch_url, which DO accept untrusted URLs.
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=3)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def initialize(self):
        session = await self._get_session()
        initialized = False
        for endpoint in self._endpoints:
            try:
                async with session.get(
                    f"{endpoint.base_url}/models",
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers=self._headers(endpoint),
                ) as resp:
                    if resp.status == 200:
                        initialized = True
                        logger.info(
                            f"Provider endpoint initialized: {endpoint.name} ({endpoint.model})"
                        )
                    else:
                        logger.warning(
                            f"Provider endpoint {endpoint.name} /models returned {resp.status}"
                        )
            except Exception as e:
                logger.error(
                    f"Provider endpoint {endpoint.name} initialization failed: {e}"
                )
        self.available = initialized
        return initialized

    async def generate_response(
        self,
        messages: list[dict],
        images: list[str] = None,
        media: list[dict] = None,
        timeout: int = 3600,
        on_tool_call_name=None,
        on_token=None,
        custom_tool_calls: bool = False,
        **kwargs,
    ) -> str:
        """Generate response. images is legacy b64 list, media is list of {b64, mime_type}.

        When the model returns native OpenAI-style ``tool_calls``, content may be
        empty. Those calls are stored on ``self._last_tool_calls`` (raw provider
        format) and ``self._last_assistant_message`` for the orchestration loop.
        Callers that pass ``tools=`` must check ``_last_tool_calls`` before treating
        empty content as a failure.

        If ``on_tool_call_name`` is provided, it's forwarded to the streaming
        layer so the caller gets a callback the moment a tool call name arrives
        mid-stream — useful for updating a live progress message during long
        generations (e.g. create_site where the model spends 20+ seconds
        generating HTML in the tool arguments).
        """
        tools = kwargs.get("tools")
        try:
            message = await self.generate_chat_completion(
                messages,
                images=images,
                media=media,
                timeout=timeout,
                on_tool_call_name=on_tool_call_name,
                on_token=on_token,
                custom_tool_calls=custom_tool_calls,
                **kwargs,
            )
        except RuntimeError as e:
            # Some endpoints reject tools/function calling with 400. Fall back to
            # a plain completion so XML tool tags still work.
            err = str(e).lower()
            if tools and (
                "tool" in err
                or "function" in err
                or "tools is not supported" in err
                or "does not support" in err
            ):
                logger.warning(
                    "Provider rejected native tools; retrying without tools: %s", e
                )
                kwargs = dict(kwargs)
                kwargs.pop("tools", None)
                message = await self.generate_chat_completion(
                    messages, images=images, media=media, timeout=timeout, **kwargs
                )
            else:
                raise

        tool_calls = message.get("tool_calls") or []
        tool_calls = tool_calls if isinstance(tool_calls, list) else []
        # Capture usage synchronously right after the await returns, before any
        # further await can let a concurrent call overwrite shared state. This
        # value is attached to the returned ProviderResult so the caller never
        # has to read the racy shared ``self._last_usage``.
        usage = dict(self._last_usage) if self._last_usage else {}
        # Keep the shared stash for backward-compat callers / tests, but callers
        # should prefer the ProviderResult attributes (race-free).
        self._last_tool_calls = tool_calls
        self._last_assistant_message = message
        content = message.get("content") or ""
        # Multimodal / some providers return content as a list of parts
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            content = "".join(parts)
        content = content if isinstance(content, str) else str(content or "")
        if not content and not tool_calls:
            raise RuntimeError("Empty response from provider")
        return ProviderResult(
            content,
            tool_calls=tool_calls,
            usage=usage,
            assistant_message=message,
        )

    async def generate_chat_completion(
        self,
        messages: list[dict],
        images: list[str] = None,
        media: list[dict] = None,
        tools: list[dict] = None,
        model: str = None,
        timeout: int = 3600,
        max_tokens: int = None,
        temperature: float = None,
        disable_reasoning: bool = None,
        fast_fallback: bool = False,
        on_tool_call_name=None,
        on_token=None,
        custom_tool_calls: bool = False,
    ) -> dict:
        """Generate an OpenAI-compatible assistant message, optionally with tools.

        If ``on_tool_call_name`` is provided, it's called (fire-and-forget) the
        first time a tool_call delta with a function name arrives in the SSE
        stream. This lets callers update a live progress message mid-generation.
        """
        if not self.available:
            logger.warning("Provider marked unavailable; retrying initialization")
            await self.initialize()
            if not self.available:
                raise RuntimeError("Provider not available")

        chat_messages = [dict(m) for m in messages]

        all_media = []
        if media:
            all_media.extend(media)
        if images:
            for img_b64 in images:
                all_media.append({"b64": img_b64, "mime_type": "image/png"})

        payload_media = []
        for m in all_media:
            mime = str(m.get("mime_type", ""))
            if not m.get("b64"):
                continue
            if mime.startswith(("image/", "video/")) or (
                mime.startswith("audio/") and getattr(self, "enable_audio_input", False)
            ):
                payload_media.append(m)

        if payload_media:
            target = None
            for msg in chat_messages:
                content = msg.get("content", "")
                if msg["role"] == "user" and (
                    "[User attached image" in content
                    or "[User attached media" in content
                    or "Media available to inspect" in content
                    or "Audio/video available to inspect" in content
                    or "Images available to inspect" in content
                    or "Server emoji/sticker reference sheet" in content
                ):
                    target = msg
                    break
            if target is None:
                for msg in reversed(chat_messages):
                    if msg["role"] == "user":
                        target = msg
                        break
            if target is not None:
                parts = [{"type": "text", "text": target.get("content", "")}]
                attached = 0
                for m in payload_media:
                    mime = m["mime_type"]
                    b64 = m["b64"]
                    uri = f"data:{mime};base64,{b64}"
                    if mime.startswith("image/"):
                        parts.append({"type": "image_url", "image_url": {"url": uri}})
                    elif mime.startswith("audio/") and getattr(
                        self, "enable_audio_input", False
                    ):
                        audio_format = AUDIO_FORMATS.get(
                            mime.split(";", 1)[0].lower(), "wav"
                        )
                        parts.append(
                            {
                                "type": "input_audio",
                                "input_audio": {"data": b64, "format": audio_format},
                            }
                        )
                    elif mime.startswith("video/"):
                        # OpenCode Go / DeepSeek reject video_url ("unknown
                        # variant"). Thumbnails and ffmpeg JPEG frames still
                        # attach as image_url.
                        logger.info(
                            "Skipping video_url part (%s); sending image frames/thumbnails only",
                            mime,
                        )
                        continue
                    else:
                        continue
                    attached += 1
                target["content"] = parts
                logger.info(f"Attached {attached} multimodal item(s) to message")
            else:
                logger.warning(
                    f"No user message found to attach {len(payload_media)} multimodal item(s)"
                )

        session = await self._get_session()
        last_error = None
        last_usage_error = None
        has_media = bool(payload_media)
        # Endpoints that rejected this call's media (text-only models 400 on
        # image_url; OpenRouter 404s on input audio). Steer retries away so a
        # GIF never dies on DeepSeek then Ling.
        media_broken: set[str] = set()
        # Endpoints that returned a deterministic non-2xx (bad model slug,
        # unsupported params). Retrying them with the same payload just repeats
        # the error, so they're excluded from the rest of this call.
        dead: set[str] = set()
        max_attempts = (
            min(self.retry_attempts, 2)
            if fast_fallback and len(self._endpoints) > 1
            else self.retry_attempts
        )
        # NOT a `for attempt in range(1, max_attempts + 1)`: several branches
        # below extend `max_attempts` mid-flight so a deterministic 4xx can be
        # handed to another endpoint (media-unsupported re-route, text-only
        # retry after stripping attachments, learned temperature resend,
        # generic non-2xx failover). range() snapshots its bounds at loop
        # entry, so every one of those extensions was a no-op: if the failure
        # landed on the final attempt the loop just fell out and the turn died
        # with "Provider call failed after retries" — exactly the failover the
        # extension was written to perform. A while loop re-reads the bound.
        # `attempt_ceiling` keeps a pathological provider (one that answers
        # every payload with a fresh deterministic 400) from looping forever.
        attempt = 0
        attempt_ceiling = max_attempts + 2 * len(self._endpoints) + 2
        while attempt < min(max_attempts, attempt_ceiling):
            attempt += 1
            endpoint = self._attempt_endpoint(
                attempt, fast_fallback=fast_fallback, has_media=has_media
            )
            if endpoint.name in media_broken or endpoint.name in dead:
                order = self._media_endpoint_order() if has_media else self._endpoints
                usable = [
                    e
                    for e in order
                    if e.name not in media_broken and e.name not in dead
                ]
                if usable:
                    endpoint = usable[0]
            data = self._request_payload(
                endpoint,
                chat_messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                disable_reasoning=disable_reasoning,
            )
            request_start = time.perf_counter()
            media_parts = sum(
                1
                for msg in chat_messages
                for part in (
                    msg.get("content") if isinstance(msg.get("content"), list) else []
                )
                if isinstance(part, dict) and part.get("type") != "text"
            )
            logger.info(
                "Provider timing start endpoint=%s model=%s attempt=%s/%s messages=%s media_parts=%s timeout=%s max_tokens=%s reasoning_disabled=%s tools=%s",
                endpoint.name,
                data.get("model"),
                attempt,
                max_attempts,
                len(chat_messages),
                media_parts,
                timeout,
                data.get("max_tokens"),
                bool(data.get("reasoning")),
                len(data.get("tools") or []),
            )
            try:
                async with session.post(
                    f"{endpoint.base_url}/chat/completions",
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=timeout, connect=10),
                    headers=self._headers(endpoint),
                ) as resp:
                    headers_ms = (time.perf_counter() - request_start) * 1000
                    if resp.status == 503:
                        error_text = await resp.text()
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                        )
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} 503",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                        ):
                            continue
                        raise RuntimeError(
                            f"Provider overloaded after retries: {error_text[:200]}"
                        )
                    if resp.status == 429:
                        error_text = await resp.text()
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                        )
                        self._cool_endpoint(endpoint.name)
                        if _is_usage_exhausted_error(resp.status, error_text):
                            last_usage_error = ProviderUsageExhaustedError(
                                f"Provider {endpoint.name} usage exhausted: {error_text[:200]}"
                            )
                            if len(self._endpoints) == 1:
                                raise last_usage_error
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} usage exhausted",
                                max_attempts=max_attempts,
                                fast_fallback=fast_fallback,
                                has_media=has_media,
                            ):
                                continue
                            raise last_usage_error
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} 429 rate limited",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                        ):
                            continue
                        raise RuntimeError(
                            f"Provider rate limited after retries: {error_text[:200]}"
                        )
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s body=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                            error_text[:200],
                        )
                        # Text-only models 400 on image_url/video_url; some
                        # fallbacks 404 on input audio. Mark broken and retry a
                        # media-capable endpoint (typically vision / primary).
                        if has_media and _is_media_unsupported_error(
                            resp.status, error_text
                        ):
                            media_broken.add(endpoint.name)
                            # Model capability, not a transient fault — remember
                            # it so later turns skip this endpoint for media.
                            self._media_incapable.add(endpoint.name)
                            order = self._media_endpoint_order()
                            usable = [e for e in order if e.name not in media_broken]
                            if not usable:
                                # Every endpoint refused the attachments. Drop
                                # them and answer the text instead of failing
                                # the whole turn.
                                if _strip_media_parts(chat_messages):
                                    logger.warning(
                                        "No endpoint accepts this media; retrying text-only: %s",
                                        error_text[:200],
                                    )
                                    has_media = False
                                    media_broken.clear()
                                    max_attempts = max(
                                        max_attempts, attempt + len(self._endpoints)
                                    )
                                    continue
                                raise RuntimeError(
                                    f"Provider {endpoint.name} media-unsupported and no alternatives: {error_text[:200]}"
                                )
                            if attempt >= max_attempts:
                                max_attempts = attempt + len(usable)
                            logger.warning(
                                "Provider endpoint %s cannot handle media; retrying with %s",
                                endpoint.name,
                                usable[0].name,
                            )
                            continue
                        # Provider-side function degradation (e.g. "DEGRADED function
                        # cannot be invoked"). This is NOT transient — don't waste
                        # retries on the same endpoint; cool it and fall back now.
                        if resp.status == 400 and "degraded" in error_text.lower():
                            self._cool_endpoint(endpoint.name)
                            logger.warning(
                                "Provider endpoint %s marked degraded; skipping to fallback",
                                endpoint.name,
                            )
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} degraded",
                                max_attempts=max_attempts,
                                fast_fallback=fast_fallback,
                                has_media=has_media,
                            ):
                                continue
                            raise RuntimeError(
                                f"Provider {endpoint.name} degraded and no fallback available: {error_text[:200]}"
                            )
                        # Region / geo blocks (DeepSeek V4 Flash China opt-in)
                        # are not transient. Don't burn a 2s retry on the same
                        # endpoint — cool it and fail over immediately.
                        if resp.status == 403 or "regionerror" in error_text.lower():
                            self._cool_endpoint(endpoint.name)
                            logger.warning(
                                "Provider endpoint %s returned 403/region block; skipping to fallback",
                                endpoint.name,
                            )
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} 403",
                                max_attempts=max_attempts,
                                fast_fallback=True,
                                has_media=has_media,
                            ):
                                continue
                            raise RuntimeError(
                                f"Provider {endpoint.name} 403 and no fallback available: {error_text[:200]}"
                            )
                        # Content-policy prompt blocks (Gemini "sensitive words
                        # that violate Google's use policy"). Not transient and
                        # not payload-shaped: the same text will be refused every
                        # time, so cool the endpoint and hand the turn to the
                        # fallback model rather than burning retries or surfacing
                        # a raw Google error into the channel.
                        if _is_content_policy_block(resp.status, error_text):
                            self._cool_endpoint(
                                endpoint.name, "blocked the prompt on content policy"
                            )
                            logger.warning(
                                "Provider endpoint %s blocked the prompt on content policy; "
                                "failing over: %s",
                                endpoint.name,
                                error_text[:200],
                            )
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} content-policy block",
                                max_attempts=max_attempts,
                                fast_fallback=True,
                                has_media=has_media,
                            ):
                                continue
                            raise RuntimeError(
                                f"Provider {endpoint.name} blocked this prompt on content "
                                f"policy and no fallback endpoint was available"
                            )
                        # Auto-clamp max_tokens on context overflow (OpenRouter returns 400)
                        if (
                            resp.status == 400
                            and "maximum context length" in error_text.lower()
                            and max_tokens is None
                        ):
                            import re as _re

                            ctx_match = _re.search(
                                r"maximum context length is (\d+) tokens", error_text
                            )
                            req_match = _re.search(
                                r"you requested about (\d+) tokens", error_text
                            )
                            if ctx_match and req_match:
                                ctx_limit = int(ctx_match.group(1))
                                requested = int(req_match.group(1))
                                estimated_input = requested - int(
                                    data.get("max_tokens", self.max_tokens)
                                )
                                safe_output = max(
                                    4096, ctx_limit - estimated_input - 512
                                )
                                if safe_output < int(
                                    data.get("max_tokens", self.max_tokens)
                                ):
                                    logger.warning(
                                        "Clamping max_tokens from %s to %s due to context limit %s",
                                        data.get("max_tokens"),
                                        safe_output,
                                        ctx_limit,
                                    )
                                    # The loop rebuilds payloads every attempt. Mutating only
                                    # data["max_tokens"] here is a fake fix; keep the clamp in
                                    # loop state or we retry the same busted request like idiots.
                                    max_tokens = safe_output
                                    data["max_tokens"] = safe_output
                                    if await self._retry_after_attempt(
                                        attempt,
                                        endpoint,
                                        f"Context overflow, clamped max_tokens to {safe_output}",
                                        max_attempts=max_attempts,
                                        fast_fallback=fast_fallback,
                                        has_media=has_media,
                                    ):
                                        continue
                        # max_tokens is *output* length, not context. Models like
                        # minimax-m3 can have 1M context but only e.g. 131072 max output.
                        if resp.status == 400 and (
                            "maximum output tokens" in error_text.lower()
                            or "exceeds model's maximum output" in error_text.lower()
                        ):
                            import re as _re

                            out_match = _re.search(
                                r"maximum output tokens\s*\(?\s*(\d+)\s*\)?",
                                error_text,
                                _re.IGNORECASE,
                            )
                            if not out_match:
                                out_match = _re.search(
                                    r"maximum output tokens \((\d+)\)",
                                    error_text,
                                    _re.IGNORECASE,
                                )
                            if out_match:
                                out_cap = int(out_match.group(1))
                                # Leave headroom under the hard cap.
                                safe_output = max(1024, min(out_cap - 64, out_cap))
                                current = int(data.get("max_tokens", self.max_tokens))
                                if safe_output < current:
                                    logger.warning(
                                        "Clamping max_tokens from %s to %s (model max output %s)",
                                        current,
                                        safe_output,
                                        out_cap,
                                    )
                                    max_tokens = safe_output
                                    # Remember per-endpoint so future calls to
                                    # this endpoint clamp proactively without a
                                    # wasted 400 round-trip. Do NOT mutate the
                                    # shared self.max_tokens: that permanently
                                    # crippled every other endpoint/concurrent
                                    # request after one small-cap model was hit.
                                    self._endpoint_output_caps[endpoint.name] = (
                                        safe_output
                                    )
                                    data["max_tokens"] = safe_output
                                    if await self._retry_after_attempt(
                                        attempt,
                                        endpoint,
                                        f"Output cap, clamped max_tokens to {safe_output}",
                                        max_attempts=max_attempts,
                                        fast_fallback=True,
                                        has_media=has_media,
                                    ):
                                        continue
                        # Some models accept exactly one temperature and 400 on
                        # anything else. Learn it and resend to the SAME endpoint
                        # rather than burning retries / falling back needlessly.
                        required_temp = _required_temperature(resp.status, error_text)
                        if (
                            required_temp is not None
                            and self._endpoint_temperatures.get(endpoint.name)
                            != required_temp
                        ):
                            logger.warning(
                                "Provider endpoint %s requires temperature=%s; resending",
                                endpoint.name,
                                required_temp,
                            )
                            # Recorded per-endpoint only. Assigning the local
                            # `temperature` override instead would carry this
                            # endpoint's constraint onto every other endpoint
                            # this call later touches.
                            self._endpoint_temperatures[endpoint.name] = required_temp
                            if attempt >= max_attempts:
                                max_attempts = attempt + 1
                            continue
                        # Anything else non-2xx used to die right here with no
                        # failover, so a 404 "model unavailable for free" on the
                        # primary killed the whole turn while a healthy fallback
                        # sat unused (logged 2026-08-07). The body is
                        # deterministic, so hand the call to a *different*
                        # endpoint — repeating it here would just 404 again.
                        # No _cool_endpoint(): a 400 from our own payload would
                        # otherwise park all traffic on the fallback for a full
                        # minute. Failing over for this call is enough.
                        dead.add(endpoint.name)
                        alternatives = [
                            e for e in self._endpoints if e.name not in dead
                        ]
                        if alternatives:
                            if attempt >= max_attempts:
                                max_attempts = attempt + len(alternatives)
                            logger.warning(
                                "Provider endpoint %s returned %s; failing over to %s",
                                endpoint.name,
                                resp.status,
                                alternatives[0].name,
                            )
                            continue
                        raise ProviderRequestError(
                            f"Provider API error: {resp.status} - {error_text}"
                        )

                    json_ms = 0.0
                    if data.get("stream"):
                        merged = await _read_sse_response(
                            resp,
                            on_tool_call_name=on_tool_call_name,
                            on_token=on_token,
                            custom_tool_calls=custom_tool_calls,
                        )
                        result = {
                            k: v for k, v in merged.items() if not k.startswith("__")
                        }
                        first_token_s = merged.get("__first_token_s__")
                        # Streaming has no JSON-parse step; report the
                        # time-to-first-token so the latency log stays useful
                        # instead of fabricating a json_ms value.
                        if first_token_s is not None:
                            json_ms = (first_token_s - request_start) * 1000
                    else:
                        result = await resp.json()
                        json_ms = (time.perf_counter() - request_start) * 1000
                    if not isinstance(result, dict):
                        result_preview = (
                            str(result)[:600] if result is not None else "None"
                        )
                        logger.warning(
                            "Provider %s returned 200 with non-dict JSON body (type=%s) preview=%s",
                            endpoint.name,
                            type(result).__name__,
                            result_preview,
                        )
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} returned non-dict JSON body",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                        ):
                            continue
                        raise RuntimeError(
                            "No response from provider (non-dict JSON body)"
                        )
                    choices = result.get("choices", [])
                    if not choices:
                        # Log details to debug providers that return 200 OK with empty choices
                        # (common with some models/endpoints on safety, overload, or format quirks).
                        result_keys = (
                            list(result.keys())
                            if isinstance(result, dict)
                            else type(result).__name__
                        )
                        result_preview = str(result)[:600] if result else ""
                        logger.warning(
                            "Provider %s returned 200 with no choices. keys=%s preview=%s",
                            endpoint.name,
                            result_keys,
                            result_preview,
                        )
                        if isinstance(result, dict) and "error" in result:
                            err_obj = result["error"]
                            logger.warning(
                                "Provider %s also included error in body: %s",
                                endpoint.name,
                                str(err_obj)[:300],
                            )
                            upstream_code = (
                                err_obj.get("code", "")
                                if isinstance(err_obj, dict)
                                else ""
                            )
                            raise RuntimeError(
                                f"No response from provider (upstream error code: {upstream_code})"
                                if upstream_code
                                else "No response from provider"
                            )
                        raise RuntimeError("No response from provider")

                    message = choices[0].get("message", {})
                    content = message.get("content") or ""
                    if isinstance(content, list):
                        content = "".join(
                            str(p.get("text") or "")
                            if isinstance(p, dict)
                            else (p if isinstance(p, str) else "")
                            for p in content
                        )
                    # Reasoning-to-content promotion.
                    #
                    # Some reasoning models (notably DeepSeek) have a quirk
                    # where the *answer* genuinely rides in `reasoning_content`
                    # with `content` left null. Commit 010b0db promoted
                    # reasoning -> content unconditionally to fix that, but it
                    # was too blunt: for an interrupted/cut-off reasoning model
                    # (grok, ollama-native, minimax-m3) a null-content + only
                    # reasoning reply is usually chain-of-thought with NO
                    # answer produced — promoting it sends the scratchpad to
                    # the channel as the user-visible reply (logged leak: the
                    # bot posted "The user is making a sexual joke about
                    # 'Bobby Fisher'… I should decline" to Discord).
                    #
                    # Rule: NEVER promote when there are tool_calls (reasoning
                    # accompanying a tool call is unambiguously internal), and
                    # NEVER promote on providers whose answers always arrive
                    # in `content`. Only promote for the known DeepSeek-family
                    # case where an empty-content answer legitimately lives in
                    # reasoning_content. Everything else drops through to the
                    # empty-response retry/fallback below instead of leaking.
                    if (
                        not content
                        and not message.get("tool_calls")
                        and self._reasoning_content_is_answer(endpoint, message)
                    ):
                        content = (
                            message.get("reasoning_content")
                            or message.get("reasoning")
                            or ""
                        )
                        if content:
                            message["content"] = content
                    # A blocked prompt comes back as a normal 200 whose content
                    # IS Google's notice. Never let that reach the channel: drop
                    # it, cool the endpoint and hand the turn to the fallback
                    # model on the very next attempt (no second try against the
                    # model that just refused — the same payload always loses).
                    if content and _is_policy_block_text(content):
                        self._cool_endpoint(
                            endpoint.name, "blocked the prompt on content policy"
                        )
                        logger.warning(
                            "Provider %s returned a content-policy prompt block as its "
                            "reply; discarding it and failing over",
                            endpoint.name,
                        )
                        content = ""
                        message["content"] = ""
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} content-policy block",
                            max_attempts=max_attempts,
                            fast_fallback=True,
                            has_media=has_media,
                        ):
                            continue
                        raise RuntimeError(
                            "Prompt was blocked by the provider's content policy and "
                            "no fallback endpoint was available"
                        )
                    if not content and not message.get("tool_calls"):
                        # Some providers return choices with a message but blank content (e.g. refusals, reasoning-only, or bugs).
                        logger.warning(
                            "Provider %s returned 200 with empty content (tool_calls=%s) message_keys=%s",
                            endpoint.name,
                            bool(message.get("tool_calls")),
                            list(message.keys())
                            if isinstance(message, dict)
                            else type(message).__name__,
                        )
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} returned empty response",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                        ):
                            continue
                        raise RuntimeError("Empty response from provider")

                    usage = result.get("usage", {})
                    self._last_usage = {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    }
                    # Healthy response: this endpoint is no longer rate-limited.
                    self._endpoint_cooldown.pop(endpoint.name, None)
                    logger.info(
                        "Provider timing done endpoint=%s status=%s headers_ms=%.1f total_ms=%.1f content_chars=%s tool_calls=%s tokens=%s",
                        endpoint.name,
                        resp.status,
                        headers_ms,
                        json_ms,
                        len(content or ""),
                        len(message.get("tool_calls") or []),
                        self._last_usage.get("total_tokens", 0),
                    )
                    return message
            except asyncio.TimeoutError:
                logger.warning(
                    "Provider timing timeout endpoint=%s elapsed_ms=%.1f timeout=%s",
                    endpoint.name,
                    (time.perf_counter() - request_start) * 1000,
                    timeout,
                )
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} timeout",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                    has_media=has_media,
                ):
                    continue
                raise RuntimeError(
                    f"Provider request timed out after {timeout}s"
                ) from asyncio.TimeoutError
            except ProviderUsageExhaustedError:
                raise
            except ProviderRequestError:
                # Deterministic and already failed over everywhere it could.
                raise
            except RuntimeError as e:
                last_error = e
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} error: {e}",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                    has_media=has_media,
                ):
                    continue
                raise
            except Exception as e:
                last_error = e
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} error: {e}",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                    has_media=has_media,
                ):
                    continue
                raise RuntimeError(f"Provider call failed: {last_error}") from e
        if last_usage_error:
            raise last_usage_error
        raise RuntimeError("Provider call failed after retries")

    async def _retry_after_attempt(
        self,
        attempt: int,
        endpoint: ProviderEndpoint,
        reason: str,
        *,
        max_attempts: int = None,
        fast_fallback: bool = False,
        has_media: bool = False,
    ) -> bool:
        max_attempts = max_attempts or self.retry_attempts
        if attempt >= max_attempts:
            return False
        next_endpoint = self._attempt_endpoint(
            attempt + 1, fast_fallback=fast_fallback, has_media=has_media
        )
        if self._should_wait_before_retry(endpoint, next_endpoint):
            wait = attempt * 2
            logger.warning(
                f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying in {wait}s..."
            )
            await asyncio.sleep(wait)
        else:
            logger.warning(
                f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying with {next_endpoint.name} provider..."
            )
        return True
