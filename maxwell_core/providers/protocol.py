"""Validate chat envelopes before output can reach the tool dispatcher."""

from dataclasses import dataclass, field
from typing import Any

from .errors import ProviderUnavailableError


class ProviderProtocolError(ProviderUnavailableError):
    """A malformed upstream response; retries remain bounded by the provider."""


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    """Read payload and clocks; payload values are excluded from diagnostics."""

    payload: Any = field(repr=False)
    ended_at: float
    first_token_s: float | None = None
    last_token_s: float | None = None


def _nullable_string(mapping: dict, key: str, *, field: str) -> None:
    value = mapping.get(key)
    if value is not None and not isinstance(value, str):
        raise ProviderProtocolError(f"Provider returned invalid {field}")


def validate_stream_choices(frame: dict) -> list[dict]:
    """Return choice zero with validated deltas and sparse integer tool indexes.

    Errors use fixed field descriptions, never raw upstream values: a BYOK
    endpoint may echo credentials in any malformed field.
    """
    choices = frame.get("choices")
    if choices is None:
        return []
    if not isinstance(choices, list):
        raise ProviderProtocolError("Provider stream returned invalid choices")
    selected = []
    for choice in choices:
        if not isinstance(choice, dict):
            raise ProviderProtocolError("Provider stream returned invalid choices")
        index = choice.get("index", 0)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ProviderProtocolError("Provider stream returned invalid choice index")
        if index != 0:
            continue
        delta = choice.get("delta")
        if delta is None:
            delta = {}
        if not isinstance(delta, dict):
            raise ProviderProtocolError("Provider stream returned invalid delta")
        _nullable_string(choice, "finish_reason", field="finish reason")
        for key in ("role", "content", "reasoning_content", "reasoning"):
            _nullable_string(delta, key, field=f"stream {key}")
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise ProviderProtocolError(
                    "Provider stream returned invalid tool calls"
                )
            for call in tool_calls:
                if not isinstance(call, dict):
                    raise ProviderProtocolError(
                        "Provider stream returned invalid tool call"
                    )
                tool_index = call.get("index", 0)
                if (
                    isinstance(tool_index, bool)
                    or not isinstance(tool_index, int)
                    or tool_index < 0
                ):
                    raise ProviderProtocolError(
                        "Provider stream returned invalid tool index"
                    )
                _validate_tool_fields(call)
        selected.append(choice)
    return selected


def _validate_tool_fields(call: dict) -> None:
    for key in ("id", "type"):
        _nullable_string(call, key, field=f"tool {key}")
    function = call.get("function")
    if function is None:
        return
    if not isinstance(function, dict):
        raise ProviderProtocolError("Provider returned invalid tool function")
    _nullable_string(function, "name", field="tool function name")
    arguments = function.get("arguments")
    if arguments is not None and not isinstance(arguments, (str, dict)):
        raise ProviderProtocolError("Provider returned invalid tool arguments")


def normalize_completion_message(choices) -> dict:
    """Normalize the first non-streamed message without mutating its envelope.

    Content parts occur on some compatible APIs even for text-only completions.
    Keep continuation metadata while ensuring callers receive a string content
    rather than calculating usage on one representation and returning another.
    """
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderProtocolError("Provider returned invalid completion choices")
    raw = choices[0].get("message")
    if not isinstance(raw, dict):
        raise ProviderProtocolError("Provider returned invalid completion message")
    message = dict(raw)
    content = message.get("content")
    if content is None:
        content = ""
    elif isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            if isinstance(part, dict)
            else part
            if isinstance(part, str)
            else ""
            for part in content
        )
    elif not isinstance(content, str):
        raise ProviderProtocolError("Provider returned invalid completion content")
    message["content"] = content
    for key in ("role", "reasoning_content", "reasoning"):
        _nullable_string(message, key, field=f"completion {key}")
    tool_calls = message.get("tool_calls")
    if tool_calls is not None and (
        not isinstance(tool_calls, list)
        or any(not isinstance(call, dict) for call in tool_calls)
    ):
        raise ProviderProtocolError("Provider returned invalid completion tool calls")
    for call in tool_calls or []:
        _validate_tool_fields(call)
    return message
