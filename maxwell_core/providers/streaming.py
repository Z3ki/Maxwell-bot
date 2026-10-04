"""Assemble OpenAI-compatible chat streams independently of transport retries."""

import json
import logging
import time

import aiohttp

from utils import _spawn_background as _fire_and_forget
from .custom_tools import _CustomToolCallBuffer
from .protocol import ProviderProtocolError, validate_stream_choices
from .sse import iter_sse_payloads
from .tool_calls import (
    _append_tool_call_arguments,
    _extract_partial_reasoning,
    _keep_tool_call_provider_fields,
)

logger = logging.getLogger(__name__)


async def _safe_call(cb, *args, **kwargs):
    """Contain callback failures without suppressing request cancellation."""
    try:
        await cb(*args, **kwargs)
    except Exception as exc:
        logger.debug("SSE callback raised: %s", exc)


class _StreamCallbacks:
    """Progress notifications cannot interrupt or back-pressure generation."""

    def __init__(self, on_tool_call_name=None, on_token=None):
        self.on_tool_call_name = on_tool_call_name
        self.on_token = on_token

    def token(self, *, content="", reasoning="", tool_name=None):
        if self.on_token is not None:
            try:
                self.on_token(
                    {"content": content, "reasoning": reasoning, "tool_name": tool_name}
                )
            except Exception as exc:
                logger.debug("on_token callback failed: %s", exc)

    def tool(self, name, reasoning=""):
        if self.on_tool_call_name is not None:
            callback = _safe_call(self.on_tool_call_name, name, reasoning)
            try:
                _fire_and_forget(callback)
            except RuntimeError:
                # Scheduling can fail during shutdown. Close the coroutine
                # instead of leaking it or awaiting a Discord edit inline.
                callback.close()

    def custom_name(self, name):
        self.token(tool_name=name)
        self.tool(name)


class _ChatStreamAssembler:
    """Accumulate one completion and continuation metadata from validated frames."""

    def __init__(self, callbacks: _StreamCallbacks, *, custom_tool_calls=False):
        self.callbacks = callbacks
        self.custom = (
            _CustomToolCallBuffer(callbacks.custom_name) if custom_tool_calls else None
        )
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict] = {}
        self.role = None
        self.finish_reason = None
        self.first_token_s = None
        self.last_token_s = None
        self.usage = None

    def _tool_delta(self, delta):
        index = delta.get("index", 0)
        slot = self.tools.setdefault(
            index,
            {
                "id": delta.get("id"),
                "type": delta.get("type", "function"),
                "function": {"name": "", "arguments": ""},
            },
        )
        for key in ("id", "type"):
            if delta.get(key):
                slot[key] = delta[key]
        _keep_tool_call_provider_fields(slot, delta)
        function = delta.get("function") or {}
        if function.get("name"):
            slot["function"]["name"] += function["name"]
            if not slot.get("_name_sent"):
                slot["_name_sent"] = True
                name = slot["function"]["name"]
                self.callbacks.tool(name)
                self.callbacks.token(tool_name=name)
        if function.get("arguments"):
            _append_tool_call_arguments(slot, function["arguments"])
            if not slot.get("_reasoning_sent"):
                reasoning = _extract_partial_reasoning(slot["function"]["arguments"])
                if reasoning:
                    slot["_reasoning_sent"] = True
                    self.callbacks.tool(slot["function"]["name"], reasoning)

    def accept(self, frame):
        choices = validate_stream_choices(frame)
        # Only generated output affects TTFT/TPS, not role/usage/finish frames.
        if any(_sse_delta_has_output(choice.get("delta") or {}) for choice in choices):
            now = time.perf_counter()
            if self.first_token_s is None:
                self.first_token_s = now
            self.last_token_s = now
        for choice in choices:
            delta = choice.get("delta") or {}
            if delta.get("role"):
                self.role = delta["role"]
            content = delta.get("content")
            visible = ""
            if content is not None:
                self.content.append(content)
                visible = (
                    self.custom.feed(content) if self.custom is not None else content
                )
            reasoning = ""
            for key in ("reasoning_content", "reasoning"):
                value = delta.get(key)
                if value is not None:
                    self.reasoning.append(value)
                if value and not reasoning:
                    reasoning = value
            if visible or reasoning:
                self.callbacks.token(content=visible, reasoning=reasoning)
            # Hidden custom JSON must not suppress simultaneous native tools.
            for tool in delta.get("tool_calls") or []:
                self._tool_delta(tool)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        if frame.get("usage"):
            self.usage = frame["usage"]

    def finish(self, *, done):
        if not done and self.finish_reason is None:
            raise ProviderProtocolError("Provider stream ended before completion")
        if (
            not self.tools
            and not self.content
            and not self.role
            and self.finish_reason is None
            and (self.custom is None or not self.custom.completed)
        ):
            raise ProviderProtocolError("Provider stream produced no choices")
        if self.custom is not None:
            self.custom.drain()
            if self.custom.completed:
                # Sparse native indexes make len(tools) an unsafe insertion key.
                next_index = max(self.tools, default=-1) + 1
                for tool in self.custom.completed:
                    self.tools[next_index] = tool
                    next_index += 1
                self.content = ["".join(self.custom.text_parts)]
        message = {"role": self.role or "assistant"}
        if self.content:
            message["content"] = "".join(self.content)
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.tools:
            message["tool_calls"] = [
                {
                    key: value
                    for key, value in self.tools[index].items()
                    if not str(key).startswith("_")
                }
                for index in sorted(self.tools)
            ]
        result = {
            "choices": [
                {"index": 0, "message": message, "finish_reason": self.finish_reason}
            ],
            "__first_token_s__": self.first_token_s,
            "__last_token_s__": self.last_token_s,
        }
        if self.usage is not None:
            result["usage"] = self.usage
        return result


async def _read_sse_response(
    resp: aiohttp.ClientResponse,
    on_tool_call_name=None,
    on_token=None,
    custom_tool_calls: bool = False,
    max_bytes: int | None = None,
) -> dict:
    """Read SSE into a non-streamed chat envelope plus first/last-output clocks.

    ``on_token`` is a synchronous notification receiving content, reasoning,
    and tool_name deltas; async ``on_tool_call_name`` runs in a retained task.
    Callbacks cannot hold up the HTTP reader. Incomplete or malformed streams
    raise before their accumulated tools can be handed to the dispatcher.
    """
    assembler = _ChatStreamAssembler(
        _StreamCallbacks(on_tool_call_name, on_token),
        custom_tool_calls=custom_tool_calls,
    )
    done = False
    async for payload in iter_sse_payloads(resp.content, max_bytes=max_bytes):
        if payload.strip() == b"[DONE]":
            done = True
            break
        if not payload.strip():
            continue
        try:
            frame = json.loads(payload)
        except ValueError as exc:
            raise ProviderProtocolError(
                "Provider stream returned invalid JSON"
            ) from exc
        if not isinstance(frame, dict):
            raise ProviderProtocolError("Provider stream returned an invalid frame")
        if frame.get("error") is not None:
            raise ProviderProtocolError("Provider stream returned an upstream error")
        assembler.accept(frame)
    return assembler.finish(done=done)


def _nonempty_output_value(val) -> bool:
    if val is None or val is False:
        return False
    if isinstance(val, str):
        return bool(val)
    if isinstance(val, (list, dict, tuple, set)):
        return bool(val)
    return True


# SSE delta keys that are protocol chrome, not generated tokens.
_SSE_PROTOCOL_DELTA_KEYS = {"role", "index"}


def _sse_delta_has_output(delta) -> bool:
    """True when this SSE delta carries generated output, not protocol chrome.

    Gemini/OpenRouter often stream thinking as ``reasoning_details`` / ``thought``
    rather than ``content``. Counting only content/tool_calls made last-token
    equal first-token, so TPS became n/a on every bursty call.
    """
    if not isinstance(delta, dict) or not delta:
        return False
    if _nonempty_output_value(delta.get("content")):
        return True
    for key in (
        "reasoning_content",
        "reasoning",
        "reasoning_details",
        "thought",
        "thinking",
        "thoughts",
        "extra_content",
        "function_call",
        "refusal",
    ):
        if _nonempty_output_value(delta.get(key)):
            return True
    for tc in delta.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        if tc.get("id") or tc.get("type"):
            return True
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        if fn and (fn.get("name") or fn.get("arguments")):
            return True
    for key, val in delta.items():
        if key in _SSE_PROTOCOL_DELTA_KEYS or key == "tool_calls":
            continue
        if _nonempty_output_value(val):
            return True
    return False
