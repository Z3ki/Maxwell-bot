"""Anthropic Messages transport adapted to Maxwell's chat-provider interface."""

import json
import time
from typing import Any
from urllib.parse import urlsplit

from providers import OpenAICompatibleProvider
from .errors import ProviderMediaUnsupportedError
from .protocol import CompletionResponse, ProviderProtocolError
from .sse import iter_sse_payloads
from .streaming import _StreamCallbacks
from .usage import _coerce_token_count


def _content_blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ProviderProtocolError("Invalid message content")
    blocks = []
    for part in content:
        if not isinstance(part, dict):
            raise ProviderProtocolError("Invalid message content part")
        kind = part.get("type")
        if kind == "text":
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url", "") if isinstance(image, dict) else image
            if not isinstance(url, str):
                raise ProviderMediaUnsupportedError("Invalid image URL")
            if url.startswith("data:"):
                header, sep, data = url.partition(",")
                if not sep or not header.endswith(";base64"):
                    raise ProviderMediaUnsupportedError("Unsupported image encoding")
                source = {"type": "base64", "media_type": header[5:-7], "data": data}
            elif urlsplit(url).scheme in {"http", "https"}:
                source = {"type": "url", "url": url}
            else:
                raise ProviderMediaUnsupportedError("Unsupported image URL")
            blocks.append({"type": "image", "source": source})
        else:
            raise ProviderMediaUnsupportedError("Anthropic does not support this media type")
    return blocks


def translate_messages(messages):
    """Translate OpenAI conversation history, preserving tool call/result IDs."""
    system = []
    translated: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer"}:
            system.extend(_content_blocks(content))
            continue
        if role == "tool":
            blocks = [{"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""),
                       "content": _content_blocks(content)}]
            if message.get("is_error"):
                blocks[0]["is_error"] = True
            role = "user"
        elif role in {"user", "assistant"}:
            blocks = _content_blocks(content)
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    arguments = function.get("arguments", "{}")
                    try:
                        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                    except (TypeError, ValueError):
                        raise ProviderProtocolError("Invalid tool call arguments") from None
                    if not isinstance(arguments, dict):
                        raise ProviderProtocolError("Invalid tool call arguments")
                    blocks.append({"type": "tool_use", "id": call.get("id", ""),
                                   "name": function.get("name", ""), "input": arguments})
        else:
            raise ProviderProtocolError("Invalid message role")
        if not blocks:
            continue
        if translated and translated[-1]["role"] == role:
            translated[-1]["content"].extend(blocks)
        else:
            translated.append({"role": role, "content": blocks})
    if any(block.get("type") != "text" for block in system):
        raise ProviderProtocolError("Invalid system content")
    return system, translated


def translate_anthropic_response(result):
    """Convert a Messages response into the existing OpenAI response envelope."""
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        raise ProviderProtocolError("Provider returned invalid Messages response")
    texts, calls = [], []
    for block in result["content"]:
        if not isinstance(block, dict):
            raise ProviderProtocolError("Provider returned invalid content block")
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise ProviderProtocolError("Provider returned invalid text")
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str) or not isinstance(block.get("input"), dict):
                raise ProviderProtocolError("Provider returned invalid tool call")
            calls.append({"id": block["id"], "type": "function", "function": {
                "name": block["name"], "arguments": json.dumps(block["input"], ensure_ascii=False)}})
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if calls:
        message["tool_calls"] = calls
    usage = result.get("usage") or {}
    if not isinstance(usage, dict):
        raise ProviderProtocolError("Provider returned invalid usage")
    prompt = sum(_coerce_token_count(usage.get(key)) for key in
                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    output = _coerce_token_count(usage.get("output_tokens"))
    normalized = {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output}
    for source, target in (("cache_read_input_tokens", "cached_tokens"),
                           ("cache_creation_input_tokens", "cache_write_tokens")):
        if source in usage:
            normalized[target] = _coerce_token_count(usage[source])
    finish_reasons: dict[str | None, str] = {
        "end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
        "tool_use": "tool_calls",
    }
    finish = finish_reasons.get(result.get("stop_reason"), result.get("stop_reason"))
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": normalized}


async def _read_anthropic_stream(resp, *, max_bytes, callbacks):
    """Assemble Messages SSE blocks without exposing thinking or partial JSON."""
    blocks: dict[int, dict[str, Any]] = {}
    arguments: dict[int, list[str]] = {}
    usage: dict[str, Any] = {}
    stop_reason = None
    completed = False
    first_token = last_token = None
    async for raw in iter_sse_payloads(resp.content, max_bytes=max_bytes):
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ProviderProtocolError("Provider returned invalid Messages stream") from None
        if not isinstance(event, dict):
            raise ProviderProtocolError("Provider returned invalid Messages stream")
        kind = event.get("type")
        if kind == "error":
            raise ProviderProtocolError("Provider returned a Messages stream error")
        if kind == "message_start":
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage", {}), dict):
                raise ProviderProtocolError("Provider returned invalid Messages stream")
            usage.update(message.get("usage") or {})
        elif kind == "content_block_start":
            index = event.get("index")
            block = event.get("content_block")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index in blocks or not isinstance(block, dict):
                raise ProviderProtocolError("Provider returned invalid content block")
            blocks[index] = dict(block)
            if block.get("type") == "tool_use":
                name = block.get("name")
                if not isinstance(name, str):
                    raise ProviderProtocolError("Provider returned invalid tool call")
                callbacks.token(tool_name=name)
                callbacks.tool(name)
            elif block.get("type") == "text" and block.get("text"):
                callbacks.token(content=block["text"])
                first_token = first_token or time.perf_counter()
                last_token = time.perf_counter()
        elif kind == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            if index not in blocks or not isinstance(delta, dict):
                raise ProviderProtocolError("Provider returned invalid content delta")
            block = blocks[index]
            if delta.get("type") == "text_delta" and block.get("type") == "text":
                text = delta.get("text")
                if not isinstance(text, str):
                    raise ProviderProtocolError("Provider returned invalid text delta")
                block["text"] = block.get("text", "") + text
                if text:
                    callbacks.token(content=text)
                    first_token = first_token or time.perf_counter()
                    last_token = time.perf_counter()
            elif delta.get("type") == "input_json_delta" and block.get("type") == "tool_use":
                fragment = delta.get("partial_json")
                if not isinstance(fragment, str):
                    raise ProviderProtocolError("Provider returned invalid tool arguments")
                arguments.setdefault(index, []).append(fragment)
                if fragment:
                    first_token = first_token or time.perf_counter()
                    last_token = time.perf_counter()
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            event_usage = event.get("usage") or {}
            if not isinstance(delta, dict) or not isinstance(event_usage, dict):
                raise ProviderProtocolError("Provider returned invalid Messages delta")
            stop_reason = delta.get("stop_reason") or stop_reason
            usage.update(event_usage)
        elif kind == "message_stop":
            completed = True
            break
    if not completed:
        raise ProviderProtocolError("Provider returned incomplete Messages stream")
    for index, fragments in arguments.items():
        try:
            blocks[index]["input"] = json.loads("".join(fragments))
        except ValueError:
            raise ProviderProtocolError("Provider returned invalid tool arguments") from None
    payload = translate_anthropic_response({
        "content": [blocks[index] for index in sorted(blocks)],
        "usage": usage,
        "stop_reason": stop_reason,
    })
    return CompletionResponse(payload, time.perf_counter(), first_token, last_token)


class AnthropicMessagesProvider(OpenAICompatibleProvider):
    completion_path = "/messages"
    extra_retryable_statuses = frozenset({500, 502, 504, 529})

    _NON_SECRET_HEADERS = OpenAICompatibleProvider._NON_SECRET_HEADERS | {"anthropic-version"}

    async def _auth_headers(self, endpoint):
        headers = await super()._auth_headers(endpoint)
        bearer = headers.pop("Authorization", "")
        if bearer:
            headers["x-api-key"] = bearer.removeprefix("Bearer ")
        headers["anthropic-version"] = "2023-06-01"
        return headers

    def _request_payload(self, endpoint, chat_messages, tools=None, model=None,
                         max_tokens=None, disable_reasoning=None):
        base = super()._request_payload(endpoint, chat_messages, model=model,
                                        max_tokens=max_tokens, disable_reasoning=disable_reasoning)
        system, messages = translate_messages(chat_messages)
        data = {"model": base["model"], "max_tokens": base["max_tokens"],
                "messages": messages, "stream": True}
        if system:
            data["system"] = system
        if tools:
            data["tools"] = [{"name": tool["function"]["name"],
                              "description": tool["function"].get("description", ""),
                              "input_schema": tool["function"].get("parameters") or {"type": "object", "properties": {}}}
                             for tool in tools]
        return data

    async def _read_completion_response(self, resp, **kwargs):
        if kwargs.get("stream"):
            callbacks = _StreamCallbacks(kwargs.get("on_tool_call_name"), kwargs.get("on_token"))
            bounded = kwargs.get("byok_request") or self.policy.max_response_bytes is not None
            return await _read_anthropic_stream(
                resp, max_bytes=self._response_limit() if bounded else None,
                callbacks=callbacks,
            )
        response = await super()._read_completion_response(resp, **kwargs)
        payload = translate_anthropic_response(response.payload)
        if not kwargs.get("byok_request"):
            callbacks = _StreamCallbacks(kwargs.get("on_tool_call_name"), kwargs.get("on_token"))
            message = payload["choices"][0]["message"]
            if message["content"]:
                callbacks.token(content=message["content"])
            for call in message.get("tool_calls", []):
                name = call["function"]["name"]
                callbacks.token(tool_name=name)
                callbacks.tool(name)
        return CompletionResponse(payload, response.ended_at,
                                  response.first_token_s, response.last_token_s)
