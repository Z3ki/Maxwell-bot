"""Protocol-neutral provider configuration, security, authentication and results."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderAuthentication(Protocol):
    """Resolve credentials at request time; implementations own their lifecycle."""

    async def headers(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class BearerAuthentication:
    token: str = field(default="", repr=False)

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


@dataclass(frozen=True)
class ProviderPolicy:
    sensitive_credentials: bool = False
    public_network_only: bool = False
    allow_redirects: bool = False
    allow_provider_fallback: bool = True
    max_request_seconds: int | None = None
    max_response_bytes: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self):
        for value in (
            self.max_request_seconds,
            self.max_response_bytes,
            self.max_output_tokens,
        ):
            if value is not None and value < 1:
                raise ValueError("Provider policy limits must be positive")
        if self.public_network_only and self.allow_redirects:
            raise ValueError("Public-only providers must block redirects")


@dataclass(frozen=True)
class ProviderCapabilities:
    text: bool = True
    streaming: bool = True
    native_tools: bool = True
    vision: bool = False
    audio: bool = False
    reasoning: bool = False
    structured_output: bool = False
    model_discovery: bool = False
    context_window: int | None = None


@dataclass(frozen=True)
class GenerationDefaults:
    max_output_tokens: int = 16384
    temperature: float = 0.7
    disable_reasoning: bool = True
    reasoning_effort: str = ""
    retry_attempts: int = 3
    empty_response_retries: int | None = 2


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str = "openai_compat"
    base_url: str = ""
    # Deprecated constructor convenience. Credentials never appear in repr.
    api_key: str = field(default="", repr=False)
    model: str = ""
    extras: dict[str, Any] = field(default_factory=dict, repr=False)
    authentication: ProviderAuthentication | None = field(default=None, repr=False)
    generation: GenerationDefaults = field(default_factory=GenerationDefaults)
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    policy: ProviderPolicy = field(default_factory=ProviderPolicy)


@dataclass(frozen=True)
class ProviderEndpoint:
    name: str
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    disable_reasoning: bool = False
    reasoning_effort: str = ""


class CompletionMessage(dict):
    """Legacy message mapping with metadata outside its protocol wire keys."""

    def __init__(
        self, message: dict, usage: dict, *, model=None, provider=None, timing=None
    ):
        super().__init__(copy.deepcopy(message))
        self.usage = copy.deepcopy(usage)
        self.model = model
        self.provider = provider
        self.timing = copy.deepcopy(timing or {})


class ProviderResult(str):
    """Per-request result; string-compatible for existing Maxwell text consumers.

    Metadata is copied so callers cannot mutate another result or the upstream
    message. No inference consumer needs shared provider response attributes.
    """

    __slots__ = (
        "tool_calls",
        "usage",
        "assistant_message",
        "model",
        "provider",
        "timing",
    )

    def __new__(
        cls,
        content,
        tool_calls=None,
        usage=None,
        assistant_message=None,
        model=None,
        provider=None,
        timing=None,
    ):
        inst = super().__new__(cls, str(content or ""))
        inst.tool_calls = copy.deepcopy(tool_calls or [])
        inst.usage = copy.deepcopy(usage or {})
        inst.assistant_message = copy.deepcopy(assistant_message)
        inst.model = model
        inst.provider = provider
        inst.timing = copy.deepcopy(timing or {})
        return inst

    @property
    def content(self) -> str:
        return str(self)
