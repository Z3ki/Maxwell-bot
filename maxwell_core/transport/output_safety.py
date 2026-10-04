"""Visible response cleanup, independent of the Discord client and turn loop."""

import json
import logging
import re
from collections.abc import Iterator

from control_defaults import KNOWN_TOOLS
from response_guard import break_echo_loop, scrub_repetitions

logger = logging.getLogger(__name__)


TOOL_TRACE_LINE_RE = re.compile(
    r"(?im)^\s*Called\s+[A-Za-z_]\w*\s+with\s+\{.*?\}\s*->\s*.+$"
)


def extract_json_object(text: str, start: int = 0) -> tuple[str, int] | None:
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != "{":
        return None
    depth = 0
    in_str = False
    j = i
    while j < len(text):
        c = text[j]
        if in_str:
            if c == "\\":
                j += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[i : j + 1], j + 1
        j += 1
    return None


KNOWN_TOOL_NAMES: frozenset[str] = frozenset(KNOWN_TOOLS) | frozenset(
    {
        "wait",
        "search_messages",
        "update_base_personality",
        "update_server_prompt",
        "email_send",
        "email_read_inbox",
        "email_get_message",
        "email_search",
    }
)

_CODE_DELIMITER_RE = re.compile(r"`+|~{3,}")
_BRACKET_TOOL_NAMES = "|".join(
    re.escape(name) for name in sorted(KNOWN_TOOL_NAMES, key=len, reverse=True)
)
_BRACKET_TOOL_BLOCK_RE = re.compile(
    rf"\[(?:TOOL_CALL:)?(?P<name>{_BRACKET_TOOL_NAMES})\]"
    r"\s*\{.*?\}\s*\[/(?:TOOL_CALL:)?(?P=name)\]",
    re.IGNORECASE | re.DOTALL,
)
_EXPLICIT_BRACKET_TOOL_TAG_RE = re.compile(
    r"\[/?TOOL_CALL:[A-Za-z_]\w*(?:[ \t]+[^\]\n]*)?\]", re.IGNORECASE
)


def _code_spans(text: str) -> Iterator[tuple[int, int, bool]]:
    """Yield complete fenced and inline code, including longer/tilde fences."""
    position = 0
    while match := _CODE_DELIMITER_RE.search(text, position):
        marker = match.group(0)
        char = re.escape(marker[0])
        fenced = len(marker) >= 3
        end_limit = len(text) if fenced else text.find("\n", match.end())
        if end_limit == -1:
            end_limit = len(text)
        width = rf"{{{len(marker)},}}" if fenced else rf"{{{len(marker)}}}"
        closing = re.compile(rf"(?<!{char}){char}{width}(?!{char})").search(
            text, match.end(), end_limit
        )
        if closing is None:
            position = match.end()
            continue
        yield match.start(), closing.end(), fenced
        position = closing.end()


def _fence_is_protocol_payload(block: str) -> bool:
    """Recognize a model's naked JSON tool envelope, rather than a code sample."""
    first_line, separator, rest = block.partition("\n")
    if not separator:
        return False
    language = first_line.lstrip("`~").strip().lower()
    if language not in {"", "json"}:
        return False
    body = re.sub(r"[`~]+$", "", rest).strip()
    if not body:
        return True
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    name = payload.get("name") or payload.get("tool") or payload.get("tool_name")
    function = payload.get("function")
    arguments = payload
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function
    elif isinstance(function, str):
        name = function
    return str(name or "").lower() in KNOWN_TOOL_NAMES and bool(
        {"arguments", "parameters", "input"} & arguments.keys()
    )


def _protect_code(
    text: str, *, drop_protocol_fences: bool = False
) -> tuple[str, list[tuple[str, str]]]:
    """Temporarily replace examples while applying prose-only leak cleanup."""
    prefix = "\ue000code"
    while prefix in text:
        prefix += "_"
    protected = []
    pieces = []
    position = 0
    for start, end, fenced in _code_spans(text):
        pieces.append(text[position:start])
        block = text[start:end]
        if not (drop_protocol_fences and fenced and _fence_is_protocol_payload(block)):
            token = f"{prefix}{len(protected)}\ue001"
            protected.append((token, block))
            pieces.append(token)
        position = end
    pieces.append(text[position:])
    return "".join(pieces), protected


def _restore_code(text: str, protected: list[tuple[str, str]]) -> str:
    for token, block in protected:
        text = text.replace(token, block)
    return text


def _find_xml_tag_end(text: str, start: int) -> int:
    quote_char = ""
    escaped = False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if quote_char:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                quote_char = ""
            continue
        if ch in {'"', "'"} and text[start:i].rstrip().endswith("="):
            quote_char = ch
        elif ch == ">":
            return i
    return -1


def _fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    return [(start, end) for start, end, _fenced in _code_spans(text or "")]


def _in_ranges(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def _parse_xml_open_tag(raw_tag: str) -> tuple[str | None, str, bool]:
    inner = raw_tag[1:-1].strip()
    if not inner or inner.startswith("/"):
        return None, "", False
    self_closing = inner.endswith("/")
    if self_closing:
        inner = inner[:-1].rstrip()
    function_match = re.match(
        r"function\s*=\s*([A-Za-z_]\w*)(?:\s+(.*))?$", inner, re.DOTALL | re.IGNORECASE
    )
    if function_match:
        return function_match.group(1), function_match.group(2) or "", self_closing
    tool_alias_match = re.match(
        r"tool:([A-Za-z_]\w*)(?:['\"]?:[A-Za-z_]\w*)?(?:\s+(.*))?$",
        inner,
        re.DOTALL | re.IGNORECASE,
    )
    if tool_alias_match:
        name = tool_alias_match.group(1)
        if name and name.lower().startswith("tool_"):
            name = name[5:]
        return name, tool_alias_match.group(2) or "", self_closing
    match = re.match(r"(?:tool:)?([A-Za-z_]\w*)(?:\s+(.*))?$", inner, re.DOTALL)
    if not match:
        return None, "", False
    name = match.group(1)
    attrs = match.group(2) or ""
    # Normalize common model mistakes like <tool_send_message> or tool_send_foo into send_message
    if name and name.lower().startswith("tool_"):
        name = name[5:]
    return name, attrs, self_closing


def _find_tool_close(text: str, name: str, start: int) -> re.Match | None:
    # Prefer named closes (</tool:name>, </name>, </tool_name>). Only fall back to
    # bare </tool>/</function> when no named close exists — otherwise a bare tag
    # inside a body (e.g. file content / HTML) closes early and steals later tools.
    n = re.escape(name)
    tn = re.escape("tool_" + name)
    tcn = re.escape("tool:" + name)
    named_re = re.compile(
        rf"</\s*(?:tool[:_])?(?:{n}|{tn}|{tcn})\s*>",
        re.IGNORECASE,
    )
    named = named_re.search(text, start)
    if named:
        return named
    bare_re = re.compile(r"</\s*(?:function|tool|tool_call)\s*>", re.IGNORECASE)
    return bare_re.search(text, start)


UNTERMINATED_TOOL_STOP_RE = re.compile(
    r"<\|end\|>|<environment_details\b|<system-reminder\b", re.IGNORECASE
)


PIPE_TOOL_RE = re.compile(
    r"<\|tool:([A-Za-z_]\w*)\s*([^>]*)>(.*?)(?:<\|/tool:\1\s*>|<\|end\|>|$)",
    re.IGNORECASE | re.DOTALL,
)


PIPE_TOOL_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*([A-Za-z_]\w*)\|>(.*?)(?:<\|tool_call_end\|>|<\|end\|>|$)",
    re.IGNORECASE | re.DOTALL,
)


GENERIC_PIPE_TOOL_RE = re.compile(
    r"<\|tool[:_]([A-Za-z_]\w*)\|>(.*?)(?=<\|[^|]*\|>|<\|/tool[:_]\1\s*\|>|<\|end[^|]*\|>|$)",
    re.IGNORECASE | re.DOTALL,
)


ARTIFACT_BLOCK_RE = re.compile(
    r"<(?:system-reminder|environment_details)\b[^>]*>.*?(?:</(?:system-reminder|environment_details)>|$)",
    re.IGNORECASE | re.DOTALL,
)


PIPE_MARKER_RE = re.compile(
    r"<\|/?(?:tool[:_][A-Za-z_]\w*|tool_call_begin|tool_call_end|end|tool_response|begin_of_text|end_of_text|start_header_id|end_header_id)\|?>",
    re.IGNORECASE,
)


LEAKED_TOOL_CALL_RE = re.compile(r"</?\s*(?:tool_call|function)\s*>", re.IGNORECASE)


LEAKED_MESSAGE_TAG_RE = re.compile(r"</?\s*message\s*>", re.IGNORECASE)


LEAKED_CONTEXT_MARKER_RE = re.compile(
    r"^\s*(?:"
    # [RESPOND TO THIS] tag — the entire line is the echoed input header
    # ("[RESPOND TO THIS] Name(id): <user's words>"), never the bot's real
    # reply, so strip the whole line. A bare stray tag is also caught.
    r"\[RESPOND TO THIS\].*"
    # Mention / reply-target metadata lines
    r"|Mentioned users in latest message:.*"
    r"|Latest message is a reply to:.*"
    # Media-manifest header lines
    r"|Images available to inspect.*"
    r"|Audio/video available to inspect.*"
    r"|Media available to inspect.*"
    # Numbered media-manifest entries: "1. IMG_1588.jpg (image/jpeg, new)"
    r"|\d+\.\s+\S+\s*\((?:image|audio|video)/[^)]+,\s*(?:new|recent)\)"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)


TRANSCRIPT_MENTION_RE = re.compile(r"@?[A-Za-z0-9_.\- ]{1,32}?\((\d{17,20})\)")


DOUBLE_WRAPPED_URL_RE = re.compile(r"<<(https?://[^>]+)>>")


WRAPPED_URL_RE = re.compile(r"<(https?://[^>\s]+)>")


TOKEN_ARTIFACT_RE = re.compile(
    r"<\|/?[^|]*tool[^|]*\|?>",
    re.IGNORECASE,
)


_DSML_INVOKE_BLOCK_RE = re.compile(
    r"<invoke\b[^>]*>.*?(?:</invoke\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)


_DSML_PARAMETER_BLOCK_RE = re.compile(
    r"<parameter\b[^>]*>.*?(?:</parameter\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)


_DSML_WRAPPED_INVOKE_RE = re.compile(
    r"<[^>]*DSML[^>]*invoke[^>]*>.*?(?:</[^>]*DSML[^>]*invoke[^>]*>|$)",
    re.IGNORECASE | re.DOTALL,
)


_DSML_WRAPPED_PARAMETER_RE = re.compile(
    r"<[^>]*DSML[^>]*parameter[^>]*>.*?(?:</[^>]*DSML[^>]*parameter[^>]*>|$)",
    re.IGNORECASE | re.DOTALL,
)


_DSML_TAG_RE = re.compile(r"</?[^>]{0,40}DSML[^>]{0,160}>", re.IGNORECASE)


_ARG_PAIR_RE = re.compile(
    r"<arg>\s*([A-Za-z_]\w*)\s*</arg>(.*?)</arg>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_arg_protocol_leaks(text: str) -> str:
    """Strip knownTool<arg>key</arg>value</arg> sequences from visible text.

    send_message keeps the inner content so a leaked blob still delivers the
    reply. Every other tool is dropped entirely.
    """
    cleaned = str(text or "")
    if "<arg>" not in cleaned.lower():
        return cleaned
    names = sorted(KNOWN_TOOL_NAMES, key=len, reverse=True)
    name_alt = "|".join(re.escape(n) for n in names)
    opener = re.compile(
        rf"(?<![A-Za-z0-9_])({name_alt})"
        r"((?:<arg>\s*[A-Za-z_]\w*\s*</arg>.*?</arg>)+)",
        re.IGNORECASE | re.DOTALL,
    )

    def _repl(match: re.Match) -> str:
        name = match.group(1).lower()
        body = match.group(2)
        if name != "send_message":
            return ""
        content = ""
        for key, value in _ARG_PAIR_RE.findall(body):
            if key.lower() == "content":
                content = value
        return content

    cleaned = opener.sub(_repl, cleaned)
    # Orphan <arg>…</arg> pairs left after a partial tool name strip.
    cleaned = _ARG_PAIR_RE.sub("", cleaned)
    return cleaned


def _unwrap_openai_text_part(text: str) -> str:
    """Collapse a leaked OpenAI content-part JSON object/array into its text.

    Models (and some provider adapters) emit the wire format
    ``{"type":"text","text":""}`` as the visible reply. Empty text is a leak;
    non-empty text is the actual message.
    """
    raw = str(text or "").strip()
    if not raw or raw[0] not in "{[":
        return text
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return text

    def _one(part) -> str | None:
        if not isinstance(part, dict):
            return None
        keys = {str(k).lower() for k in part}
        if keys <= {"type", "text"} and "text" in part:
            typ = str(part.get("type") or "text").lower()
            if typ in {"text", ""}:
                return str(part.get("text") or "")
        return None

    if isinstance(parsed, dict):
        inner = _one(parsed)
        return inner if inner is not None else text
    if isinstance(parsed, list) and parsed:
        parts = [_one(p) for p in parsed]
        if all(p is not None for p in parts):
            return "".join(parts)
    return text


def _unwrap_reply_envelopes(text: str) -> str:
    """Decode nested text/content envelopes before examining visible prose.

    Each decoded layer is shorter than its JSON representation. Iteration
    avoids recursion depth limits and ensures later cleanup sees actual text,
    rather than escaped protocol markers inside another envelope.
    """
    current = text
    while True:
        unwrapped = _unwrap_openai_text_part(current)
        if unwrapped == current and current.strip().startswith("{"):
            try:
                payload = json.loads(current)
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = None
            if isinstance(payload, dict) and len(payload) == 1:
                key = next(iter(payload))
                if key.lower() == "content":
                    unwrapped = str(payload[key] or "")
        if unwrapped == current:
            return current
        current = unwrapped


def _strip_leading_reasoning_json(text: str) -> str:
    extracted = extract_json_object(text)
    if not extracted:
        return text
    raw_json, end = extracted
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as _exc:
        return text
    if not isinstance(payload, dict) or not (
        {"thoughts", "intent", "decision", "tool_plan"} & set(payload)
    ):
        return text
    return text[end:].lstrip()


_DSML_INVOKE_NAME_RE = re.compile(
    r"""\bname\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


_DSML_CONTENT_PARAM_RE = re.compile(
    r"<[^>]*parameter[^>]*\bname\s*=\s*['\"]content['\"][^>]*>(.*?)</[^>]*parameter[^>]*>",
    re.IGNORECASE | re.DOTALL,
)


def _replace_dsml_invoke(match: re.Match) -> str:
    """Keep send_message content; drop every other DSML invoke block."""
    block = match.group(0)
    name_m = _DSML_INVOKE_NAME_RE.search(block)
    name = (name_m.group(1) if name_m else "").strip().lower()
    if name != "send_message":
        return ""
    contents = [m.group(1).strip() for m in _DSML_CONTENT_PARAM_RE.finditer(block)]
    return contents[-1] if contents else ""


def _strip_dsml_tool_leaks(text: str) -> str:
    """Drop DeepSeek DSML dumps; keep leaked send_message content as the reply."""
    cleaned = str(text or "")
    before = cleaned
    cleaned = _DSML_WRAPPED_INVOKE_RE.sub(_replace_dsml_invoke, cleaned)
    cleaned = _DSML_INVOKE_BLOCK_RE.sub(_replace_dsml_invoke, cleaned)
    cleaned = _DSML_WRAPPED_PARAMETER_RE.sub("", cleaned)
    cleaned = _DSML_PARAMETER_BLOCK_RE.sub("", cleaned)
    cleaned = _DSML_TAG_RE.sub("", cleaned)
    if cleaned != before:
        logger.warning(
            "Stripped DeepSeek DSML tool leak (%d chars)",
            len(before) - len(cleaned),
        )
        leftover = cleaned.strip()
        names = sorted(KNOWN_TOOL_NAMES, key=len, reverse=True)
        name_alt = "|".join(re.escape(n) for n in names)
        if leftover and re.fullmatch(name_alt, leftover, flags=re.IGNORECASE):
            cleaned = ""
        else:
            cleaned = re.sub(
                rf"^(?:{name_alt})\s*\n+",
                "",
                leftover,
                count=1,
                flags=re.IGNORECASE,
            )
    return cleaned


def strip_model_artifact_leaks(text: str, strip_pipe_markers: bool = True) -> str:
    cleaned, protected = _protect_code(str(text or ""))
    cleaned = _strip_leading_reasoning_json(cleaned)
    cleaned = ARTIFACT_BLOCK_RE.sub("", cleaned)
    if strip_pipe_markers:
        cleaned = PIPE_MARKER_RE.sub("", cleaned)
        cleaned = TOKEN_ARTIFACT_RE.sub("", cleaned)
    cleaned = LEAKED_TOOL_CALL_RE.sub("", cleaned)
    cleaned = LEAKED_MESSAGE_TAG_RE.sub("", cleaned)
    # Always strip these garbage tokens; they are never valid visible output.
    cleaned = re.sub(
        r"<\|?end_of_text\|?>|<\|?tool_response\|?>|<unk>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return _restore_code(cleaned, protected)


def _iter_top_level_tool_tags(response: str, available_tools: set[str] | None = None):
    text = str(response or "")
    available_lower = (
        {n.lower() for n in available_tools} if available_tools is not None else None
    )
    code_ranges = _fenced_code_ranges(text)
    pipe_matches = []
    for match in PIPE_TOOL_RE.finditer(text):
        if _in_ranges(match.start(), code_ranges):
            continue
        name = match.group(1)
        if name and name.lower().startswith("tool_"):
            name = name[5:]
        if available_lower is None or name.lower() in available_lower:
            pipe_matches.append(
                (
                    match.start(),
                    match.end(),
                    name,
                    match.group(2),
                    match.group(3),
                    False,
                )
            )
    for match in PIPE_TOOL_CALL_RE.finditer(text):
        if _in_ranges(match.start(), code_ranges):
            continue
        name = match.group(1)
        if name and name.lower().startswith("tool_"):
            name = name[5:]
        if available_lower is None or name.lower() in available_lower:
            pipe_matches.append(
                (match.start(), match.end(), name, match.group(2), "", True)
            )
    for match in GENERIC_PIPE_TOOL_RE.finditer(text):
        if _in_ranges(match.start(), code_ranges):
            continue
        name = match.group(1)
        if name and name.lower().startswith("tool_"):
            name = name[5:]
        if available_lower is None or name.lower() in available_lower:
            pipe_matches.append(
                (match.start(), match.end(), name, "", match.group(2), False)
            )
    # De-dupe overlapping pipe matches (PIPE_TOOL_RE + GENERIC_PIPE_TOOL_RE can both
    # match the same <|tool:name|>… span and cause double execution). Prefer the
    # longer span, then first match order.
    if pipe_matches:
        pipe_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        deduped = []
        occupied: list[tuple[int, int]] = []
        for m in pipe_matches:
            start, end = m[0], m[1]
            if any(not (end <= os_ or start >= oe) for os_, oe in occupied):
                continue
            occupied.append((start, end))
            deduped.append(m)
        pipe_matches = sorted(deduped, key=lambda x: (x[0], x[1]))
        # Continue to XML scan for non-overlapping regions; do not early-return
        # so mixed pipe+XML batches still work.
        for m in pipe_matches:
            yield m
        # Build occupied ranges so XML parser skips pipe-covered spans.
        pipe_ranges = [(m[0], m[1]) for m in pipe_matches]
    else:
        pipe_ranges = []
    pos = 0
    while pos < len(text):
        start = text.find("<", pos)
        if start == -1:
            break
        if _in_ranges(start, code_ranges) or _in_ranges(start, pipe_ranges):
            containing = next(
                (
                    end
                    for range_start, end in (code_ranges + pipe_ranges)
                    if range_start <= start < end
                ),
                start + 1,
            )
            pos = containing
            continue
        # Skip tool-looking tags that sit inside quoted JSON / string literals
        # (e.g. {"thoughts":"<tool:shell .../>"}). Still allow glued tags after
        # letters/punctuation: "ship<tool:create_site ...>" — the old
        # whitespace-only rule dropped those and leaked HTML into Discord.
        if start > 0 and text[start - 1] in {'"', "'", "`", "\\"}:
            pos = start + 1
            continue
        tag_end = _find_xml_tag_end(text, start)
        if tag_end == -1:
            break
        name, attrs_str, self_closing = _parse_xml_open_tag(text[start : tag_end + 1])
        if not name or (
            available_lower is not None and name.lower() not in available_lower
        ):
            pos = start + 1
            continue
        if self_closing:
            yield start, tag_end + 1, name, attrs_str, "", True
            pos = tag_end + 1
            continue
        close_match = _find_tool_close(text, name, tag_end + 1)
        if not close_match:
            stop_match = UNTERMINATED_TOOL_STOP_RE.search(text, tag_end + 1)
            body_end = stop_match.start() if stop_match else len(text)
            # Do not claim the entire rest of the response — only up to body_end —
            # so later tools are still discoverable.
            yield start, body_end, name, attrs_str, text[tag_end + 1 : body_end], False
            pos = body_end
            continue
        yield (
            start,
            close_match.end(),
            name,
            attrs_str,
            text[tag_end + 1 : close_match.start()],
            False,
        )
        pos = close_match.end()


def strip_tool_payload_leaks(text: str) -> str:
    # First remove any full tool invocation blocks (XML or pipe) including their payloads.
    # This must happen before token stripping so that <|tool_foo|>body  removes body too.
    original = str(text or "")
    cleaned, protected = _protect_code(
        _unwrap_reply_envelopes(original), drop_protocol_fences=True
    )
    cleaned = _strip_arg_protocol_leaks(cleaned)
    cleaned = _strip_dsml_tool_leaks(cleaned)
    ranges = [
        (start, end)
        for start, end, *_rest in _iter_top_level_tool_tags(cleaned, KNOWN_TOOL_NAMES)
    ]
    # Pipe and XML parsers yield independently. Delete by source position so
    # an earlier removal cannot invalidate the offsets of a later payload.
    for start, end in sorted(ranges, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    # Now clean remaining artifacts/markers on the leftovers.
    cleaned = strip_model_artifact_leaks(cleaned)
    # Final safety for any stray tokens left.
    cleaned = TOKEN_ARTIFACT_RE.sub("", cleaned)
    cleaned = PIPE_MARKER_RE.sub("", cleaned)
    cleaned = re.sub(
        r"<\|?[^<>\|\s]{0,30}tool[^<>\|\s]{0,30}\|?>", "", cleaned, flags=re.IGNORECASE
    )
    # Extra defensive: strip common leaked reasoning blocks that escape other passes
    # (some models leak <think> or raw JSON decision objects into visible text).
    cleaned = re.sub(
        r"<think\b[^>]*>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL
    )
    # Strip leading JSON decision/tool-call blocks. The previous trigger set
    # missed models that invent their own keys ("reasoning", "name", "arguments",
    # "emoji") and emit the raw tool JSON as their visible reply. Match any JSON
    # object that LOOKS like a tool invocation: has both a "name"/"tool" key and
    # an "arguments"/"parameters" key, OR has a "thoughts" key.
    tool_call_obj_re = re.compile(
        r"\{(?:[^{}]|\{[^{}]*\})*?"
        r"(?:\"name\"|\"tool\"|\"tool_name\"|\"function\")"
        r"(?:[^{}]|\{[^{}]*\})*?"
        r"(?:\"arguments\"|\"parameters\"|\"input\")"
        r"(?:[^{}]|\{[^{}]*\})*\}",
        re.IGNORECASE | re.DOTALL,
    )
    cleaned = tool_call_obj_re.sub("", cleaned)
    # Also catch decision objects that have just a "reasoning" / "intent" / etc key
    # but no proper arguments block — the model is leaking its scratchpad.
    decision_obj_re = re.compile(
        r"^\s*\{[\s\S]*?"
        r"(?:\"thoughts\"|\"intent\"|\"decision\"|\"tool_plan\"|\"reasoning\"|"
        r"\"internal_monologue\"|\"plan\"|\"action_plan\")"
        r"[\s\S]*?\}\s*",
        re.IGNORECASE,
    )
    cleaned = decision_obj_re.sub("", cleaned)
    # Final catch-all: if a reply is *just* a JSON object (possibly with surrounding
    # whitespace / quotes), treat it as a leak. Real replies don't start with `{`.
    cleaned = _unwrap_openai_text_part(cleaned)
    if cleaned.strip().startswith("{") and cleaned.strip().endswith("}"):
        try:
            parsed = json.loads(cleaned.strip())
            if isinstance(parsed, dict):
                tool_keys = {
                    "name",
                    "tool",
                    "tool_name",
                    "function",
                    "arguments",
                    "parameters",
                    "input",
                    "emoji",
                    "reasoning",
                    "thoughts",
                    "intent",
                    "decision",
                    "tool_plan",
                    "internal_monologue",
                }
                # Response-envelope keys: the model sometimes emits a fake
                # response object {"content": "...", "reply": true} as its
                # visible reply instead of just the content string.
                envelope_keys = {
                    "content",
                    "reply",
                    "text",
                    "message",
                    "response",
                    "channel",
                    "recipient",
                    "user_id",
                    "message_id",
                    "recipient_id",
                    "target",
                    "send",
                    "should_reply",
                }
                keys = {k.lower() for k in parsed}
                tool_hits = sum(1 for k in parsed if k.lower() in tool_keys)
                env_hits = sum(1 for k in parsed if k.lower() in envelope_keys)
                # 3) Single-key object with "content" -> the model forgot to
                # strip the envelope, keep the inner text. Must run BEFORE the
                # blanket envelope-strip below, otherwise the single content
                # key matches the env-keys set and gets nuked.
                if len(parsed) == 1 and "content" in keys:
                    cleaned = str(parsed["content"] or "")
                # 1) Any tool-shaped key, small dict -> nuke
                # 2) Pure response envelope (all keys are envelope-shaped) -> nuke
                elif (tool_hits >= 1 and len(parsed) <= 8) or (
                    env_hits == len(parsed) and len(parsed) <= 6
                ):
                    cleaned = ""
        except Exception as e:
            # Not JSON after all — leave the text as-is.
            logger.debug("Envelope strip skipped (unparseable): %s", e)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    # 2026-07-21: the LLM (minimax-m3) sometimes echoes a Discord
    # user-message header into its visible reply, then continues
    # with the actual answer — or stops right there, in which case
    # the bot ends up sending the previous user message as its own
    # reply. Patterns observed:
    #   - "DisplayName (@handle)(id): text"
    #   - "DisplayName (@handle) (id): text"
    #   - "DisplayName (id): text"
    #   - "@DisplayName (id): text"
    # Strip the leading line if it matches this format. The
    # heuristic is "looks like a Discord mention-prefixed line" —
    # a real reply never starts with a paren-id group. Only fires
    # at the start of the reply so a model that wants to @mention
    # a user mid-reply isn't impacted.
    user_header_re = re.compile(
        r"^\s*"
        r"@?[A-Za-z0-9_.\-]{1,32}"  # name or @handle (no spaces)
        r"(?:\s*\(\s*@?[A-Za-z0-9_.\-]{1,32}\s*\))?"  # optional (@handle) group
        r"\s*"
        r"\(\d{17,20}\)\s*"  # required (id)
        r"(?:\(\d{17,20}\)\s*)?"  # optional 2nd (id) (e.g. log-format duplicates)
        r":[ \t]*[^\n]*\n+"
    )
    cleaned = user_header_re.sub("", cleaned, count=1).strip()
    # 2026-07-23: strip any leaked internal context markers (mention/reply/
    # media-manifest lines, [RESPOND TO THIS] tags) the model echoed back.
    # These are code-generated and never valid visible output.
    cleaned = LEAKED_CONTEXT_MARKER_RE.sub("", cleaned)
    # 2026-07-25: convert transcript-format mentions (@Name(snowflake_id) or
    # Name(snowflake_id)) to proper Discord pings (<@snowflake_id>), and fix
    # double/single-wrapped URLs the model emits (<<url>> → url, <url> → url).
    cleaned = DOUBLE_WRAPPED_URL_RE.sub(r"\1", cleaned)
    cleaned = WRAPPED_URL_RE.sub(r"\1", cleaned)
    cleaned = TRANSCRIPT_MENTION_RE.sub(r"<@\1>", cleaned)
    # After a fenced tool JSON body is stripped, the model often leaves
    # ```json\n\n``` behind. Discord posted that empty fence as the reply.
    cleaned = re.sub(r"(?:```|~~~)[^\n`~]{0,32}\s*(?:```|~~~)", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) < len(original) * 0.95 and logger.isEnabledFor(logging.DEBUG):
        # Significant sanitization happened; helps debug persistent leak issues without always logging.
        logger.debug(
            "strip_tool_payload_leaks removed %d chars of artifacts",
            len(original) - len(cleaned),
        )
    return _restore_code(cleaned, protected)


def _sanitize_visible_reply(text: str, *, scrub_repeats: bool = True) -> str:
    """Shared Discord cleanup for leaked tool traces and sent-markers.

    Also the one place output repetition is collapsed. `response_guard` was
    written for exactly that — "jajajajajaja" down to "ja", a sentence said
    twice down to once — and had passing tests, but nothing ever called it, so
    every run of it reached the channel intact. This is the choke point both
    transports already share, which is why the call belongs here rather than
    in each send path.
    """
    raw, protected = _protect_code(
        _unwrap_reply_envelopes(str(text or "")), drop_protocol_fences=True
    )
    if "\\n" in raw:
        raw = raw.replace("\\n", "\n")
    # Ordinary brackets and Markdown links are user content. Strip only
    # actual paired tool payloads or explicitly labeled tool-call tags.
    response = _BRACKET_TOOL_BLOCK_RE.sub("", raw)
    response = _EXPLICIT_BRACKET_TOOL_TAG_RE.sub("", response)
    response = TOOL_TRACE_LINE_RE.sub("", response)
    for marker in (
        "__NO_RESPONSE__",
        "__SHELL_SENT__",
        "__MEME_SENT__",
        "__MEDIA_SENT__",
        "__FILE_SENT__",
        "__MESSAGE_SENT__",
        "__REASONING_RECORDED__",
    ):
        response = response.replace(marker, "")
    response = strip_tool_payload_leaks(response).strip()
    if scrub_repeats and response:
        # Collapses laugh runs, doubled words, repeated sentences and repeated
        # phrases — never inside fenced code, so a program that legitimately
        # repeats a line is untouched.
        response = scrub_repetitions(response)
        # A model that has fallen into an echo loop emits the same trigram
        # over and over until it hits the token cap. Truncating at the first
        # repeat leaves the useful prefix instead of posting the whole loop.
        response = break_echo_loop(response)
    return _restore_code(response, protected)


def _auto_format_discord(text: str) -> str:
    if not text or len(text.strip()) < 10:
        return text
    # 2026-07-23: removed the URL-wrapping pass that wrapped every link in
    # <angle brackets>. That killed Discord embeds/previews for every link
    # the bot posted (e.g. <https://maxwell.z3ki.dev/bot/love-letter>), which
    # the operator flagged. Links are now sent raw so Discord renders them
    # normally with previews. The markdown early-return is kept as a hook for
    # future formatting logic.
    return text
