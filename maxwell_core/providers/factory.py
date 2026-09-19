"""Build a chat provider from config without the turn loop knowing the class.

The default implementation wraps the existing OpenAI-compatible client in
``providers.py``. A plugin can register ``provider:<name>`` on the service
container to replace a slot.
"""

from __future__ import annotations

from typing import Any

from .base import ChatProvider, ProviderCapabilities, ProviderConfig


def openai_compat_provider(
    *,
    base_url: str,
    api_key: str = "",
    model: str = "",
    name: str = "primary",
    **kwargs: Any,
) -> Any:
    from providers import OllamaProvider

    client = OllamaProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        **kwargs,
    )
    client.name = name
    client.capabilities = ProviderCapabilities(
        text=True,
        streaming=True,
        native_tools=True,
        vision=True,
        audio=True,
        reasoning=True,
        model_discovery=True,
    )
    return client


def provider_from_config(config: ProviderConfig, **kwargs: Any) -> ChatProvider | Any:
    kind = (config.kind or "openai_compat").strip().lower()
    if kind in {"openai_compat", "openai", "ollama", "openrouter", "lmstudio", "custom"}:
        return openai_compat_provider(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            name=config.name,
            **kwargs,
        )
    raise ValueError(f"unknown provider kind {config.kind!r}")
