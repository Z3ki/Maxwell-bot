"""Incremental SSE framing independent of chat-completion payloads.

Keep bytes intact until an event is assembled so network boundaries inside
UTF-8 characters, CRLF delimiters, and multiline data do not alter the JSON.
"""

import re
from collections.abc import AsyncIterator

_LINE_END = re.compile(rb"\r\n|\r|\n")
_UTF8_BOM = b"\xef\xbb\xbf"


class SSEDecoder:
    """Decode data events, ignoring comments and other SSE fields."""

    def __init__(self):
        self._buffer = b""
        self._data: list[bytes] = []
        self._first_line = True

    def _accept_line(self, line: bytes) -> bytes | None:
        if self._first_line:
            self._first_line = False
            if line.startswith(_UTF8_BOM):
                line = line[len(_UTF8_BOM) :]
        if not line:
            if not self._data:
                return None
            payload = b"\n".join(self._data)
            self._data.clear()
            return payload
        field, separator, value = line.partition(b":")
        if field == b"data":
            if separator and value.startswith(b" "):
                value = value[1:]
            self._data.append(value)
        return None

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        events = []
        consumed = 0
        for match in _LINE_END.finditer(self._buffer):
            # CRLF may straddle two reads. Wait for the next byte before
            # treating a trailing CR as a complete delimiter.
            if match.group() == b"\r" and match.end() == len(self._buffer):
                break
            payload = self._accept_line(self._buffer[consumed : match.start()])
            consumed = match.end()
            if payload is not None:
                events.append(payload)
        self._buffer = self._buffer[consumed:]
        return events

    def finish(self) -> list[bytes]:
        """Flush an EOF tail for compatible servers omitting the final blank line.

        The chat assembler still requires [DONE] or a finish reason. Flushing
        therefore cannot turn a truncated generation into a complete reply.
        """
        events = self.feed(b"\n") if self._buffer else []
        payload = self._accept_line(b"")
        if payload is not None:
            events.append(payload)
        return events


async def iter_sse_payloads(
    content, *, max_bytes: int | None = None
) -> AsyncIterator[bytes]:
    """Yield complete SSE data events within the entire response byte budget."""
    decoder = SSEDecoder()
    received = 0
    async for chunk in content.iter_any():
        received += len(chunk)
        if max_bytes is not None and received > max_bytes:
            raise RuntimeError("Provider response exceeded the configured size limit")
        for payload in decoder.feed(chunk):
            yield payload
    for payload in decoder.finish():
        yield payload
