"""Wire-level regressions for untrusted OpenAI-compatible event streams."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from providers import _read_sse_response


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_any(self):
        for chunk in self.chunks:
            yield chunk


def read(chunks, **kwargs):
    return asyncio.run(
        _read_sse_response(SimpleNamespace(content=Stream(chunks)), **kwargs)
    )


def event(delta, *, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def encode(*frames):
    return (
        b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames)
        + b"data: [DONE]\n\n"
    )


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("multiline", [False, True])
def test_sse_reassembles_every_byte_boundary_with_bom_and_comments(newline, multiline):
    frame = event({"content": "Hola, piñón 🐈"}, finish="stop")
    payload = json.dumps(frame, ensure_ascii=False).encode()
    if multiline:
        payload = payload.replace(b'"delta":', b'\n"delta":')
    data_lines = newline.join(b"data: " + line for line in payload.split(b"\n"))
    wire = (
        b"\xef\xbb\xbf: keepalive"
        + newline
        + b"event: message"
        + newline
        + data_lines
        + newline * 2
        + b"data: [DONE]"
        + newline * 2
    )
    expected = "Hola, piñón 🐈"
    assert read([wire])["choices"][0]["message"]["content"] == expected
    # This includes splits inside UTF-8 codepoints, the BOM, and CRLF pairs.
    for split in range(1, len(wire)):
        result = read([wire[:split], wire[split:]])
        assert result["choices"][0]["message"]["content"] == expected
    assert (
        read([bytes([byte]) for byte in wire])["choices"][0]["message"]["content"]
        == expected
    )


@pytest.mark.parametrize("indices", [[1], [0, 2], [1_000_000]])
def test_sparse_native_indexes_do_not_overwrite_calls_when_custom_calls_merge(indices):
    native = [
        {
            "index": index,
            "id": f"native-{index}",
            "function": {"name": f"native_{index}", "arguments": "{}"},
        }
        for index in indices
    ]
    custom = json.dumps({"name": "custom", "arguments": {"query": "weather"}})
    wire = encode(event({"content": custom, "tool_calls": native}, finish="tool_calls"))
    calls = read([wire], custom_tool_calls=True)["choices"][0]["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == [
        *(f"native_{index}" for index in sorted(indices)),
        "custom",
    ]
    assert (
        read([wire], custom_tool_calls=True)["choices"][0]["message"]["content"] == ""
    )


@pytest.mark.parametrize("index", [-1, True, "0", 0.5, {}, []])
def test_invalid_tool_index_fails_before_returning_dispatchable_calls(index):
    wire = encode(
        event(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "function": {"name": "send_message", "arguments": "{}"},
                    }
                ]
            },
            finish="tool_calls",
        )
    )
    with pytest.raises(RuntimeError, match="invalid tool"):
        read([wire])


@pytest.mark.parametrize(
    "delta",
    [
        {"content": ["text"]},
        {"content": 9},
        {"reasoning": {}},
        {"role": []},
        {"tool_calls": "bad"},
        {"tool_calls": {"index": 0}},
        {"tool_calls": [None]},
        {"tool_calls": [{"function": "bad"}]},
        {"tool_calls": [{"function": {"name": 4}}]},
        {"tool_calls": [{"function": {"arguments": [1, 2]}}]},
    ],
)
def test_malformed_delta_raises_protocol_error_instead_of_partial_result(delta):
    with pytest.raises(RuntimeError, match="invalid"):
        read([encode(event(delta, finish="stop"))])


def test_sse_size_limit_counts_ignored_lines_and_rejects_at_boundary():
    wire = (
        b": " + b"x" * 100 + b"\n\n" + encode(event({"content": "ok"}, finish="stop"))
    )
    assert read([wire], max_bytes=len(wire))["choices"][0]["message"]["content"] == "ok"
    with pytest.raises(RuntimeError, match="size limit"):
        read([wire], max_bytes=len(wire) - 1)


def test_callback_failure_does_not_prevent_the_other_custom_callback():
    async def run():
        calls = []

        def broken_token(_delta):
            raise RuntimeError("Discord edit failed")

        async def tool_name(name, reasoning):
            calls.append((name, reasoning))

        wire = encode(
            event(
                {"content": '{"name":"web_search","arguments":{}}'}, finish="tool_calls"
            )
        )
        result = await _read_sse_response(
            SimpleNamespace(content=Stream([wire])),
            on_token=broken_token,
            on_tool_call_name=tool_name,
            custom_tool_calls=True,
        )
        await asyncio.sleep(0)
        assert calls == [("web_search", "")]
        assert (
            result["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            == "web_search"
        )

    asyncio.run(run())


def test_done_marker_stops_reading_without_waiting_for_another_network_chunk():
    async def run():
        class NeverEnds:
            async def iter_any(self):
                yield encode(event({"content": "ok"}, finish="stop"))
                await asyncio.Event().wait()

        result = await asyncio.wait_for(
            _read_sse_response(SimpleNamespace(content=NeverEnds())), timeout=0.1
        )
        assert result["choices"][0]["message"]["content"] == "ok"

    asyncio.run(run())
