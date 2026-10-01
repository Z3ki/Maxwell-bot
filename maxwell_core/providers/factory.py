"""Assemble protocol clients and independent routing slots from configuration."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .base import ChatProvider
from .models import GenerationDefaults, ProviderCapabilities, ProviderConfig
from .routing import ProviderRouter

OPENAI_COMPAT_KINDS = frozenset(
    {"openai_compat", "openai", "ollama", "openrouter", "groq", "lmstudio", "custom"}
)


def provider_from_config(config: ProviderConfig, **kwargs: Any) -> ChatProvider:
    from providers import OpenAICompatibleProvider

    kind = (config.kind or "openai_compat").strip().lower()
    if kind not in OPENAI_COMPAT_KINDS:
        raise ValueError(f"unknown provider kind {config.kind!r}")
    defaults = config.generation
    options = dict(config.extras)
    options.update(
        max_tokens=defaults.max_output_tokens,
        temperature=defaults.temperature,
        disable_reasoning=defaults.disable_reasoning,
        reasoning_effort=defaults.reasoning_effort,
        retry_attempts=defaults.retry_attempts,
        empty_response_retries=defaults.empty_response_retries,
    )
    options.update(kwargs)
    options.setdefault("enable_audio_input", config.capabilities.audio)
    if any(
        options.get(key)
        for key in ("fallback_base_url", "fallback_model", "vision_model")
    ):
        raise ValueError(
            "Construct alternate providers separately and use ProviderRouter"
        )
    return OpenAICompatibleProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        policy=config.policy,
        authentication=config.authentication,
        config=config,
        **options,
    )


def openai_compat_provider(
    *,
    base_url: str,
    api_key: str = "",
    model: str = "",
    name: str = "primary",
    **kwargs: Any,
) -> ChatProvider:
    """Legacy keyword assembly facade; convert once to explicit configurations."""
    policy = kwargs.pop("policy", None)
    authentication = kwargs.pop("authentication", None)
    fallback_url = kwargs.pop("fallback_base_url", "")
    fallback_model = kwargs.pop("fallback_model", "")
    fallback_key = kwargs.pop("fallback_api_key", "")
    fallback_reasoning = kwargs.pop("fallback_disable_reasoning", True)
    fallback_effort = kwargs.pop("fallback_reasoning_effort", "")
    vision_url = kwargs.pop("vision_base_url", "")
    vision_model = kwargs.pop("vision_model", "")
    vision_key = kwargs.pop("vision_api_key", "")
    vision_reasoning = kwargs.pop("vision_disable_reasoning", True)
    defaults = GenerationDefaults(
        max_output_tokens=kwargs.pop("max_tokens", 16384),
        temperature=kwargs.pop("temperature", 0.7),
        disable_reasoning=kwargs.pop("disable_reasoning", True),
        reasoning_effort=kwargs.pop("reasoning_effort", ""),
        retry_attempts=kwargs.pop("retry_attempts", 3),
        empty_response_retries=kwargs.pop("empty_response_retries", None),
    )
    # A None recovery count retains the client's environment/default lookup.
    from .models import ProviderPolicy

    caps = ProviderCapabilities(
        vision=True,
        audio=kwargs.get("enable_audio_input", False),
        reasoning=True,
        model_discovery=True,
    )
    config = ProviderConfig(
        name=name,
        base_url=base_url,
        api_key=api_key,
        model=model,
        generation=defaults,
        capabilities=caps,
        policy=policy or ProviderPolicy(),
        authentication=authentication,
        extras=kwargs,
    )
    primary = provider_from_config(config)
    fallback = vision = None
    if fallback_url and fallback_model:
        fallback = provider_from_config(
            replace(
                config,
                name="fallback",
                base_url=fallback_url,
                model=fallback_model,
                api_key=fallback_key,
                authentication=None,
                generation=replace(
                    defaults,
                    disable_reasoning=fallback_reasoning,
                    reasoning_effort=fallback_effort,
                ),
            )
        )
    if vision_model:
        vision = provider_from_config(
            replace(
                config,
                name="vision",
                base_url=vision_url or base_url,
                model=vision_model,
                api_key=vision_key or api_key,
                authentication=authentication if not vision_key else None,
                generation=replace(
                    defaults, disable_reasoning=vision_reasoning, reasoning_effort=""
                ),
            )
        )
    if fallback or vision:
        return ProviderRouter(
            primary,
            fallback,
            vision,
            retry_attempts=defaults.retry_attempts,
            empty_response_retries=primary.empty_response_retries,
            cooldown_seconds=primary._cooldown_seconds,
        )
    return primary
