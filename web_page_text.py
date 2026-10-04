"""Readable source text, including sections linked by HTML fragments."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import unquote


class _PageText(HTMLParser):
    def __init__(self, fragment: str):
        super().__init__(convert_charrefs=True)
        self.fragment = unquote(fragment)
        self.parts: list[str] = []
        self.skip: list[str] = []
        self.anchor: int | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "nav", "footer", "aside"}:
            self.skip.append(tag)
        if self.skip:
            return
        attributes = dict(attrs)
        if (
            self.fragment
            and self.anchor is None
            and self.fragment in {attributes.get("id"), attributes.get("name")}
        ):
            self.anchor = len(self.parts)
        if tag in {
            "br",
            "p",
            "div",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "section",
            "article",
        }:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in {
            "p",
            "div",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "section",
            "article",
        }:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def extract_page_text(source: str, fragment: str = "") -> str:
    parser = _PageText(fragment)
    parser.feed(source)
    parser.close()
    # A link to faq#supported-devices should start at that section instead
    # of losing it behind 15,000 characters of unrelated FAQ text.
    parts = parser.parts[parser.anchor :] if parser.anchor is not None else parser.parts
    text = "".join(parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
