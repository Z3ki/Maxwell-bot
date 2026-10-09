"""Routing between independently configured providers, above protocol clients."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import deque
from dataclasses import replace

from .base import ChatProvider
from .errors import (
    ProviderEmptyResponseError,
    ProviderError,
    ProviderMediaUnsupportedError,
    ProviderUnavailableError,
)
from .models import CompletionMessage, ProviderPolicy

logger = logging.getLogger(__name__)


class ProviderRouter(ChatProvider):
    """Keep routing budgets, cooldowns and credential sources outside clients.

    Protocol clients own payloads, transport, learned model constraints and
    authentication. Every attempt returns its own result. A future native
    protocol provider can occupy any slot without changing Discord handling.
    """

    def __init__(
        self,
        primary: ChatProvider,
        fallback: ChatProvider | None = None,
        vision: ChatProvider | None = None,
        *,
        retry_attempts: int = 3,
        empty_response_retries: int = 2,
        cooldown_seconds: float = 60,
    ):
        self.primary = primary
        self.fallback = fallback
        self.vision = vision
        self.name = primary.name
        self.model = getattr(primary, "model", "")
        self.capabilities = replace(
            primary.capabilities,
            vision=primary.capabilities.vision
            or bool(vision and vision.capabilities.vision),
            audio=primary.capabilities.audio
            or bool(vision and vision.capabilities.audio),
        )
        self.policy = getattr(primary, "policy", ProviderPolicy())
        if not self.policy.allow_provider_fallback and (fallback or vision):
            raise ValueError("Provider policy forbids alternate credentials/routes")
        if len({p.name for p in self.providers}) != len(self.providers):
            raise ValueError("Provider routing names must be distinct")
        self.retry_attempts = max(1, min(retry_attempts, 10))
        self.empty_response_retries = max(0, min(empty_response_retries, 5))
        self.cooldown_seconds = max(0, cooldown_seconds)
        self._cooldowns: dict[str, float] = {}
        self._media_incapable: set[str] = set()
        self._timing_history = deque(maxlen=100)
        self.available = False

    @property
    def providers(self) -> tuple[ChatProvider, ...]:
        return tuple(
            p for p in (self.primary, self.fallback, self.vision) if p is not None
        )

    async def initialize(self):
        for provider in self.providers:
            try:
                await provider.initialize()
            except Exception:
                # Initialization exceptions may contain credential-bearing URLs.
                logger.warning("Provider %s could not initialize", provider.name)
        self.available = any(getattr(p, "available", True) for p in self.providers)
        return self.available

    async def close(self):
        # Close all sessions even if one cleanup fails.
        results = await asyncio.gather(
            *(p.close() for p in self.providers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Provider session cleanup failed")

    def _order(self, has_media: bool, prefer_fallback: bool):
        first = self.vision if has_media and self.vision else self.primary
        order = [first]
        if self.fallback:
            order.append(self.fallback)
        if has_media and self.primary not in order:
            order.append(self.primary)
        if prefer_fallback and self.fallback in order:
            order.remove(self.fallback)
            order.insert(0, self.fallback)
        return sorted(
            order, key=lambda p: has_media and p.name in self._media_incapable
        )

    async def generate_response(
        self, messages, *, prefer_fallback=False, fast_fallback=False, **kwargs
    ):
        has_media = bool(kwargs.get("media") or kwargs.get("images")) or any(
            isinstance(part, dict)
            and part.get("type") in {"image_url", "input_audio", "video_url"}
            for message in messages
            for part in (
                message.get("content")
                if isinstance(message.get("content"), list)
                else []
            )
        )
        messages = copy.deepcopy(messages)
        order = self._order(has_media, prefer_fallback)
        dead: set[str] = set()
        max_attempts = (
            min(self.retry_attempts, 2)
            if fast_fallback and len(order) > 1
            else self.retry_attempts
        )
        # A media request can have vision, fallback AND primary routes. Reserve
        # an attempt for each rather than retrying only the first two forever.
        if len(order) > 2:
            max_attempts = max(max_attempts, len(order) + (0 if fast_fallback else 1))
        ceiling = max_attempts + 2 * len(order) + 2 + self.empty_response_retries
        empty_recoveries = 0
        attempt = 0
        recovery_provider = None
        last_error: ProviderError | None = None
        while attempt < min(max_attempts, ceiling):
            attempt += 1
            split = 1 if fast_fallback else 2
            route_index = max(0, attempt - split)
            natural = order[min(route_index, len(order) - 1)]
            candidates = [p for p in order if p.name not in dead]
            if not candidates:
                break
            healthy = [
                p
                for p in candidates
                if self._cooldowns.get(p.name, 0) <= time.monotonic()
            ]
            selected = recovery_provider or natural
            recovery_provider = None
            if selected not in candidates or (selected not in healthy and healthy):
                selected = (healthy or candidates)[0]
            options = dict(kwargs)
            # Overrides belong to the primary model, never a fallback/vision model.
            if selected is not self.primary:
                options.pop("model", None)
            # Neutral per-request retry/recovery controls avoid nested budgets.
            options.update(
                retry_attempts=1, empty_response_retries=0, allow_media_degrade=False
            )
            if empty_recoveries:
                options["stream"] = False
            # Keep model identity at the end of the prompt, leaving the
            # stable cached prefix untouched. Each retry gets its own snapshot.
            effective_model = (
                (options.get("model") or selected.model)
                if selected is self.primary else selected.model
            )
            attempt_messages = copy.deepcopy(messages)
            model_context = {
                "role": "system",
                "content": (
                    "Runtime model for this request: "
                    f"{effective_model}. Route: {selected.name}. "
                    "This is the active model for the current response, "
                    "even if earlier turns used a different model. "
                    "Do not claim another model is currently running."
                ),
            }
            insert_at = (
                len(attempt_messages) - 1
                if attempt_messages and attempt_messages[-1].get("role") == "user"
                else len(attempt_messages)
            )
            attempt_messages.insert(insert_at, model_context)
            try:
                result = await selected.generate_response(attempt_messages, **options)
                self._cooldowns.pop(selected.name, None)
                self.available = True
                if result.timing:
                    self._timing_history.append(copy.deepcopy(result.timing))
                return result
            except ProviderError as error:
                last_error = error
                if error.cooldown:
                    self._cooldowns[selected.name] = (
                        time.monotonic() + self.cooldown_seconds
                    )
                if isinstance(error, ProviderMediaUnsupportedError):
                    self._media_incapable.add(selected.name)
                    dead.add(selected.name)
                    if all(p.name in dead for p in order):
                        # Preserve the existing final text-only degradation.
                        from providers import _strip_media_parts

                        _strip_media_parts(messages)
                        kwargs.pop("images", None)
                        kwargs.pop("media", None)
                        messages.append({
                            "role": "system",
                            "content": "Media attachments are unavailable: no configured model accepted them. No attachment pixels or audio were delivered. Do not describe unseen media; explain this limitation if the answer depends on it.",
                        })
                        has_media = False
                        dead.clear()
                        order = self._order(False, prefer_fallback)
                    max_attempts = max(max_attempts, attempt + len(order))
                elif not error.retryable:
                    dead.add(selected.name)
                    max_attempts = max(max_attempts, attempt + len(order) - len(dead))
                if (
                    isinstance(error, ProviderEmptyResponseError)
                    and attempt >= max_attempts
                ):
                    if empty_recoveries < self.empty_response_retries:
                        empty_recoveries += 1
                        max_attempts = attempt + 1
                        recovery_provider = next(
                            (p for p in candidates if p is not selected), selected
                        )
                    else:
                        break
                if attempt >= max_attempts:
                    break
                # Only back off when another attempt must use the same upstream.
                next_index = max(0, attempt + 1 - split)
                next_natural = order[min(next_index, len(order) - 1)]
                next_candidates = [p for p in order if p.name not in dead]
                next_healthy = [
                    p
                    for p in next_candidates
                    if self._cooldowns.get(p.name, 0) <= time.monotonic()
                ]
                next_selected = recovery_provider or next_natural
                if next_candidates and (
                    next_selected not in next_candidates
                    or (next_selected not in next_healthy and next_healthy)
                ):
                    next_selected = (next_healthy or next_candidates)[0]
                if next_selected is selected and error.retryable:
                    await asyncio.sleep(min(8, attempt * 1.5))
        if last_error:
            raise last_error
        raise ProviderUnavailableError(
            "No available provider could complete the request"
        )

    async def generate_chat_completion(self, messages, **kwargs):
        result = await self.generate_response(messages, **kwargs)
        return CompletionMessage(
            result.assistant_message
            or {"role": "assistant", "content": result.content},
            result.usage,
            model=result.model,
            provider=result.provider,
            timing=result.timing,
        )
