"""Request-scoped links for web-search answers, independent of model compliance."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from urllib.parse import quote, urlsplit


WEB_REFERENCE_INSTRUCTION = (
    "After web_search, the final visible answer must include clickable numbered "
    "references, for example [1](<https://example.com/page>), next to the claims "
    "they support or in a short References line. This applies to send_message "
    "and plain replies. Use the returned reference numbers and URLs; never "
    "invent sources. If no usable results exist, say so without fabricating links."
)
_references: ContextVar[list[str] | None] = ContextVar("web_references", default=None)
_MAX_REFERENCES = 20


def begin_web_references() -> Token:
    return _references.set([])


def reset_web_references(token: Token) -> None:
    _references.reset(token)


def _source_url(value: object) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 1500 or any(ord(c) < 32 for c in url):
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
    except ValueError:
        return ""
    # Keep Discord markdown delimiters and whitespace out of link destinations.
    escaped = quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")
    return escaped if len(escaped) <= 1500 else ""


def _links(urls: list[str]) -> str:
    # One link per line lets both Discord chunkers preserve long URLs intact.
    return "\n".join(f"[{i}](<{url}>)" for i, url in enumerate(urls, 1))


def record_web_search_hits(hits: list[dict]) -> str:
    """Register structured result URLs, never links embedded in snippets."""
    urls = _references.get()
    if urls is None:
        urls = []  # Direct tool calls still get citation instructions.
    for hit in hits:
        url = _source_url(hit.get("href") or hit.get("url"))
        if url and url not in urls and len(urls) < _MAX_REFERENCES:
            urls.append(url)
    if not urls:
        return ""
    return "\n\n" + WEB_REFERENCE_INSTRUCTION + "\nWeb references: " + _links(urls)


def ensure_web_references(text: str) -> str:
    """Keep valid model citations; append search references when they are missing."""
    urls = _references.get()
    if not text or not text.strip() or not urls:
        return text
    # A code example is not a visible source citation for the answer.
    visible = re.sub(r"```.*?(?:```|$)|`[^`\n]*`", "", text, flags=re.S)
    citations = re.findall(
        r"\[(\d{1,3})\]\((?:<(https?://[^\s<>]+)>|(https?://[^\s)]+))\)", visible
    )
    for number, wrapped, bare in citations:
        index = int(number) - 1
        if 0 <= index < len(urls) and _source_url(wrapped or bare) == urls[index]:
            return text
    # These are search references, not invented claim-to-source associations.
    answer = text.rstrip()
    if answer.count("```") % 2:
        answer += "\n```"
    return answer + "\n\nSearch references: " + _links(urls)
