"""Provider-neutral chat interface.

The live OpenAI-compatible client remains in ``providers.py``. New providers
register on the service container / plugin context rather than editing the
turn loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


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


@dataclass
class ProviderConfig:
    name: str
    kind: str = "openai_compat"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


class ChatProvider(ABC):
    """Minimal surface the execution pipeline depends on."""

    name: str = "provider"
    capabilities: ProviderCapabilities = ProviderCapabilities()

    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def generate_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def generate_chat_completion(self, *args: Any, **kwargs: Any) -> Any:
        return await self.generate_response(*args, **kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def close(self) -> None:
        return None

    def declare(self) -> dict[str, Any]:
        caps = self.capabilities
        return {
            "name": self.name,
            "text": caps.text,
            "streaming": caps.streaming,
            "native_tools": caps.native_tools,
            "vision": caps.vision,
            "audio": caps.audio,
            "reasoning": caps.reasoning,
            "structured_output": caps.structured_output,
            "model_discovery": caps.model_discovery,
            "context_window": caps.context_window,
        }
