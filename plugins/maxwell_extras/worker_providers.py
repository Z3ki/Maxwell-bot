"""Independent, request-scoped worker connections. Never route around a saved key."""

from __future__ import annotations

import asyncio

from provider_limits import WORKER_TIMEOUT_MAX_SECONDS, worker_timeout_seconds
from .byok import make_request_provider


async def select_worker_provider(bot, user_id, inherited=None):
    """Return (client, owned): dedicated BYOK, chat BYOK, operator, then chat.

    A corrupt/disabled saved credential raises instead of silently spending a
    different account's credit or sending its task to another destination.
    """
    for attribute in ("_worker_byok_vault", "_byok_vault"):
        if attribute == "_byok_vault" and inherited is not None:
            return inherited, False
        vault = getattr(bot, attribute, None)
        if vault is not None and await asyncio.to_thread(vault.has_credential, user_id):
            credential = await asyncio.to_thread(vault.get, user_id)
            if credential:
                return make_request_provider(bot, credential), True
    config = getattr(bot, "config", None)
    model = getattr(config, "AI_WORKER_MODEL", "").strip()
    endpoint = getattr(config, "AI_WORKER_BASE_URL", "").strip()
    if endpoint and not model:
        raise ValueError("AI_WORKER_MODEL is required with AI_WORKER_BASE_URL")
    if not model:
        return getattr(bot, "ai_provider", None), False
    # A new endpoint must never receive the primary provider's secret implicitly.
    key = getattr(config, "AI_WORKER_API_KEY", None)
    if key is None:
        key = "" if endpoint else getattr(config, "AI_API_KEY", "")
    from maxwell_core.providers.models import ProviderPolicy

    output = getattr(config, "AI_WORKER_MAX_OUTPUT_TOKENS", 8192)
    provider = bot._make_chat_provider(
        name="worker",
        base_url=endpoint or getattr(config, "AI_BASE_URL", ""),
        api_key=key,
        model=model,
        max_tokens=output,
        context_window=getattr(config, "AI_WORKER_CONTEXT_WINDOW", 0) or None,
        disable_reasoning=getattr(config, "AI_WORKER_DISABLE_REASONING", True),
        reasoning_effort=getattr(config, "AI_WORKER_REASONING_EFFORT", ""),
        retry_attempts=3,
        empty_response_retries=0,
        policy=ProviderPolicy(
            allow_provider_fallback=False,
            max_request_seconds=getattr(
                config, "AI_WORKER_REQUEST_TIMEOUT_SECONDS", 600
            ),
            max_output_tokens=output,
        ),
    )
    # Ceiling only; the job budget (default or model-requested) applies below it.
    provider.turn_timeout_seconds = max(
        worker_timeout_seconds(bot), WORKER_TIMEOUT_MAX_SECONDS
    )
    provider.available = True
    return provider, True
