"""OpenAI-compatible chat transport and compatibility helpers for Maxwell."""

import asyncio
import copy
from functools import wraps
import ipaddress
import logging
import os
import time
from collections import deque
from typing import Any

import random as _random

import aiohttp

from maxwell_core.providers.base import ChatProvider
from maxwell_core.providers.models import (
    BearerAuthentication,
    CompletionMessage as _CompletionMessage,
    ProviderAuthentication,
    ProviderCapabilities,
    ProviderConfig,
    ProviderEndpoint,
    ProviderPolicy,
    ProviderResult,
)
from maxwell_core.providers.errors import (
    ProviderError,
    ProviderAuthenticationError,
    ProviderEmptyResponseError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
    ProviderMediaUnsupportedError,
    ProviderUsageExhaustedError,
)

# Compatibility exports. New code may import cohesive helpers from maxwell_core.providers.
from maxwell_core.providers.http import (
    _ip_is_public as _ip_is_public,
    _PublicOnlyResolver as _PublicOnlyResolver,
    _read_response_text_limited as _read_response_text_limited,
    _read_json_response_limited as _read_json_response_limited,
    normalize_base_url as normalize_base_url,
)
from maxwell_core.providers.custom_tools import (
    _CustomToolCallBuffer as _CustomToolCallBuffer,
    _find_balanced_json_end as _find_balanced_json_end,
    _repair_unescaped_html_quotes as _repair_unescaped_html_quotes,
    _body_terminator_candidates as _body_terminator_candidates,
    _escape_body_slice as _escape_body_slice,
    _safe_parse_tool_call_candidate as _safe_parse_tool_call_candidate,
)
from maxwell_core.providers.tool_calls import (
    _keep_tool_call_provider_fields as _keep_tool_call_provider_fields,
    _append_tool_call_arguments as _append_tool_call_arguments,
    _extract_partial_reasoning as _extract_partial_reasoning,
)
from maxwell_core.providers.streaming import (
    _safe_call as _safe_call,
    _read_sse_response as _read_sse_response,
    _nonempty_output_value as _nonempty_output_value,
    _sse_delta_has_output as _sse_delta_has_output,
)
from maxwell_core.providers.usage import (
    _coerce_token_count as _coerce_token_count,
    _first_present_token_count as _first_present_token_count,
    _reported_cost_usd as _reported_cost_usd,
    _normalize_llm_usage as _normalize_llm_usage,
    _estimate_completion_tokens as _estimate_completion_tokens,
)
from maxwell_core.providers.timing import (
    compute_llm_timing as compute_llm_timing,
    format_timing_debug as format_timing_debug,
    _decode_tps as _decode_tps,
    _weighted_tps as _weighted_tps,
    format_timing_reply_line as format_timing_reply_line,
    append_timing_to_reply as append_timing_to_reply,
    _format_timing_row as _format_timing_row,
)
from maxwell_core.providers.error_rules import (
    _is_usage_exhausted_error as _is_usage_exhausted_error,
    _is_policy_block_text as _is_policy_block_text,
    _is_content_policy_block as _is_content_policy_block,
    _is_media_unsupported_error as _is_media_unsupported_error,
    _required_temperature as _required_temperature,
    _is_stream_options_rejected as _is_stream_options_rejected,
    context_output_limit,
    maximum_output_limit,
)
from maxwell_core.providers.protocol import (
    CompletionResponse,
    normalize_completion_message,
)

logger = logging.getLogger(__name__)

# When an endpoint returns a 429 (rate-limited / usage-exhausted), we temporarily
# steer traffic away from it for this long instead of retrying it in the same
# request. This avoids hammering a shared upstream pool (e.g. OpenRouter's
# pooled free keys) that is already rate-limiting us, which only makes the
# limit worse. Override via AI_ENDPOINT_COOLDOWN_SECONDS.
DEFAULT_ENDPOINT_COOLDOWN_SECONDS = 60.0
# A provider can acknowledge a request with HTTP 200 and still emit no
# assistant content or tool call. Keep ordinary retries small, but give this
# specific transient response a separate, bounded recovery round.
DEFAULT_EMPTY_RESPONSE_RETRIES = 2
TIMING_HISTORY_MAX = 24


USAGE_EXHAUSTED_MESSAGE = (
    "The api is down cuz yall drained the usage and im not rich so wait like 2 hours"
)

AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}

MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".apng": "image/apng",
    ".mp4": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".m4v": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
}


def _strip_media_parts(chat_messages: list[dict]) -> bool:
    """Flatten multimodal content back to plain text. True if anything changed.

    Last resort when every endpoint rejects the attachments: sending the text
    alone beats dropping the user's message on the floor.
    """
    changed = False
    for msg in chat_messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        dropped = len(content) - len(texts)
        merged = "\n".join(t for t in texts if t).strip()
        if dropped > 0:
            merged = (
                f"{merged}\n[{dropped} attachment(s) omitted — "
                "no available model could accept them]"
            ).strip()
        msg["content"] = merged
        changed = True
    return changed


def _bounded_provider_request(method):
    """Enforce a policy deadline across initialization, retries and cleanup."""

    @wraps(method)
    async def bounded(self, *args, **kwargs):
        if self.policy.max_request_seconds is None:
            return await method(self, *args, **kwargs)
        try:
            async with asyncio.timeout(self.policy.max_request_seconds):
                return await method(self, *args, **kwargs)
        except TimeoutError:
            raise ProviderUnavailableError(
                "Provider request exceeded its time limit"
            ) from None

    return bounded


class OpenAICompatibleProvider(ChatProvider):
    """OpenAI-compatible chat client.

    New code constructs one coherent upstream per client via the factory.
    Multi-endpoint constructor arguments and _last_* attributes are deprecated
    compatibility paths for integrations predating ProviderRouter.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        max_tokens: int,
        temperature: float,
        api_key: str = "",
        disable_reasoning: bool = True,
        fallback_base_url: str = "",
        fallback_model: str = "",
        fallback_api_key: str = "",
        fallback_disable_reasoning: bool = True,
        fallback_reasoning_effort: str = "",
        retry_attempts: int = 3,
        enable_audio_input: bool = False,
        vision_base_url: str = "",
        vision_model: str = "",
        vision_api_key: str = "",
        vision_disable_reasoning: bool = True,
        empty_response_retries: int | None = None,
        reasoning_effort: str = "",
        policy: ProviderPolicy | None = None,
        authentication: ProviderAuthentication | None = None,
        config: ProviderConfig | None = None,
    ):
        self._policy = policy or ProviderPolicy()
        self.authentication = authentication or BearerAuthentication(api_key.strip())
        self.config = config
        self.name = config.name if config else "primary"
        self.capabilities = (
            config.capabilities
            if config
            else ProviderCapabilities(
                vision=True,
                audio=enable_audio_input,
                reasoning=True,
                model_discovery=True,
            )
        )
        if not self.policy.allow_provider_fallback and (fallback_model or vision_model):
            raise ValueError("Provider policy forbids alternate credentials/routes")
        self._validate_endpoint(base_url)
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.reasoning_effort = (reasoning_effort or "").strip()
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = api_key.strip()
        self.retry_attempts = max(1, retry_attempts)
        if empty_response_retries is None:
            try:
                empty_response_retries = int(
                    os.getenv(
                        "AI_EMPTY_RESPONSE_RETRIES",
                        os.getenv(
                            "OLLAMA_EMPTY_RESPONSE_RETRIES",
                            str(DEFAULT_EMPTY_RESPONSE_RETRIES),
                        ),
                    )
                    or DEFAULT_EMPTY_RESPONSE_RETRIES
                )
            except (TypeError, ValueError):
                empty_response_retries = DEFAULT_EMPTY_RESPONSE_RETRIES
        self.empty_response_retries = max(0, min(int(empty_response_retries), 5))
        self.enable_audio_input = bool(enable_audio_input)
        self._endpoints = [
            ProviderEndpoint(
                "primary",
                self.base_url,
                self.model,
                self.api_key,
                disable_reasoning,
                self.reasoning_effort,
            )
        ]
        if fallback_base_url and fallback_model:
            self._endpoints.append(
                ProviderEndpoint(
                    "fallback",
                    normalize_base_url(fallback_base_url),
                    fallback_model,
                    fallback_api_key.strip(),
                    fallback_disable_reasoning,
                    (fallback_reasoning_effort or "").strip(),
                )
            )
        # Appended last so text routing can keep treating index 1 as fallback.
        vision_model = (vision_model or "").strip()
        if vision_model:
            self._endpoints.append(
                ProviderEndpoint(
                    "vision",
                    normalize_base_url(vision_base_url or self.base_url),
                    vision_model,
                    (vision_api_key or self.api_key).strip(),
                    vision_disable_reasoning,
                )
            )
        self._session = None
        self._session_lock = asyncio.Lock()
        self.available = False
        # Deprecated snapshots for diagnostics/legacy callers; never inference inputs.
        self._last_usage: dict = {}
        self._last_tool_calls: list = []
        self._last_assistant_message: dict | None = None
        self._last_timing: dict = {}
        self._timing_history: deque = deque(maxlen=TIMING_HISTORY_MAX)
        # Learned model constraints are isolated by endpoint and actual model:
        # a primary model override must not constrain normal or fallback calls.
        self._endpoint_output_caps: dict[tuple[str, str], int] = {}
        # Same idea for models that accept exactly one temperature (Console Go
        # rejects anything but 0.6 with a 400). Learned once, applied up front.
        self._endpoint_temperatures: dict[tuple[str, str], float] = {}
        # Endpoints that 400 on stream_options.include_usage (older Ollama).
        # Learned once, then we stop sending it and fall back to estimating
        # completion tokens from the output text.
        self._endpoints_without_stream_usage: set[str] = set()
        # Endpoints that have proven they cannot accept attachments (e.g. a
        # text-only fallback like inclusionai/ling-3.0-flash 404ing with "No
        # endpoints found that support image input"). Remembered across calls
        # so every subsequent image turn skips them instead of re-paying for
        # the same round-trip. Whether an endpoint's model is multimodal does
        # not change between requests, so this never needs to expire.
        self._media_incapable: set[str] = set()
        # Per-endpoint rate-limit cooldown: name -> monotonic expiry. While an
        # endpoint is cooling, _attempt_endpoint steers to an alternative (if
        # any) so a rate-limited upstream isn't retried immediately.
        self._endpoint_cooldown: dict[str, float] = {}
        try:
            self._cooldown_seconds = float(
                os.getenv(
                    "AI_ENDPOINT_COOLDOWN_SECONDS",
                    os.getenv(
                        "OLLAMA_ENDPOINT_COOLDOWN_SECONDS",
                        str(DEFAULT_ENDPOINT_COOLDOWN_SECONDS),
                    ),
                )
                or DEFAULT_ENDPOINT_COOLDOWN_SECONDS
            )
        except (TypeError, ValueError):
            self._cooldown_seconds = DEFAULT_ENDPOINT_COOLDOWN_SECONDS

    @property
    def policy(self) -> ProviderPolicy:
        return self._policy

    def _validate_endpoint(self, base_url: str) -> None:
        if not self.policy.public_network_only:
            return
        from urllib.parse import urlsplit

        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Public-only providers require HTTPS without URL credentials"
            )
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return
        if not address.is_global:
            raise ValueError("Provider address must be public")

    async def _auth_headers(self, endpoint: ProviderEndpoint) -> dict[str, str]:
        headers = self._headers(endpoint)
        if endpoint.name == "primary":
            # The injectable source is authoritative, including no auth.
            headers.pop("Authorization", None)
            try:
                headers.update(await self.authentication.headers())
            except Exception:
                raise ProviderAuthenticationError(
                    "Unable to obtain provider credentials"
                ) from None
        return headers

    def _display_error(self, detail: Any, secrets: tuple[str, ...] = ()) -> str:
        """Never copy BYOK upstream bodies into logs or surfaced exceptions."""
        if self.policy.sensitive_credentials:
            return "provider rejected or could not complete the request"
        text = str(detail or "")
        for secret in (*secrets, self.api_key):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def _redact_provider_payload(
        self, value: Any, secrets: tuple[str, ...] = ()
    ) -> Any:
        """Remove a BYOK credential if a malicious upstream echoes it back."""
        if not self.policy.sensitive_credentials:
            return value
        if isinstance(value, str):
            for secret in secrets or (self.api_key,):
                if secret:
                    value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [self._redact_provider_payload(item, secrets) for item in value]
        if isinstance(value, dict):
            return {
                self._redact_provider_payload(
                    key, secrets
                ): self._redact_provider_payload(item, secrets)
                for key, item in value.items()
            }
        return value

    def _response_limit(self) -> int:
        try:
            configured = int(self.policy.max_response_bytes or 10 * 1024 * 1024)
        except (TypeError, ValueError):
            configured = 10 * 1024 * 1024
        return max(1, min(configured, 10 * 1024 * 1024))

    def _headers(self, endpoint: ProviderEndpoint = None) -> dict[str, str]:
        api_key = self.api_key if endpoint is None else endpoint.api_key
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # OpenCode Go 400s chat completions without x-opencode-session
        # (MissingSessionID, enforced 2026-09-06). A stable per-install id
        # is enough; catalog GET /models does not require it.
        base = (self.base_url if endpoint is None else endpoint.base_url) or ""
        if "opencode.ai" in base.lower():
            session = (
                os.getenv("OPENCODE_SESSION")
                or os.getenv("AI_OPENCODE_SESSION")
                or os.getenv("OLLAMA_OPENCODE_SESSION")  # deprecated alias
                or "maxwell"
            ).strip()
            if session:
                headers["x-opencode-session"] = session
        return headers

    def _endpoint_named(self, name: str) -> ProviderEndpoint | None:
        for ep in self._endpoints:
            if ep.name == name:
                return ep
        return None

    def _reasoning_content_is_answer(
        self,
        endpoint: ProviderEndpoint | None,
        message: dict,
    ) -> bool:
        """Return True only when this provider's reasoning_content holds a real
        user-facing answer rather than internal chain-of-thought.

        A null `content` + non-empty `reasoning_content` is ambiguous: some
        models (DeepSeek-family) put the actual answer in reasoning_content;
        an interrupted/cut-off reasoning model (grok, ollama-native,
        minimax-m3) leaves only scratchpad there with no answer at all.
        Promoting scratchpad to content is what leaked the bot's reasoning to
        Discord. We only trust reasoning_content as an answer for the models
        that are known to ship text that way; for everything else we let the
        empty-response retry/fallback take over.
        """
        model = (endpoint.model if endpoint is not None else self.model) or ""
        m = model.lower()
        # DeepSeek-family convention: answer may ride in reasoning_content.
        return any(tok in m for tok in ("deepseek", "deep_seek", "deepseek-r1"))

    def _media_endpoint_order(self) -> list[ProviderEndpoint]:
        """Vision first, then the rest. Text-only primaries 400 on image_url."""
        vision = self._endpoint_named("vision")
        ordered: list[ProviderEndpoint] = []
        if vision is not None:
            ordered.append(vision)
        for ep in self._endpoints:
            if ep not in ordered:
                ordered.append(ep)
        # Endpoints already known to reject attachments go last rather than
        # being dropped: if they're all we have left, a doomed try still beats
        # refusing to send anything.
        return sorted(ordered, key=lambda ep: ep.name in self._media_incapable)

    def _attempt_endpoint(
        self,
        attempt: int,
        *,
        fast_fallback: bool = False,
        has_media: bool = False,
        prefer_fallback: bool = False,
    ) -> ProviderEndpoint:
        primary = self._endpoint_named("primary") or self._endpoints[0]
        fallback = self._endpoint_named("fallback")
        vision = self._endpoint_named("vision")

        if has_media and vision is not None:
            # A fallback that has already proven text-only is not a media
            # option; sending it an image_url just buys another 404.
            if fallback is not None and fallback.name in self._media_incapable:
                fallback = None
            if prefer_fallback and fallback is not None:
                natural = (
                    fallback
                    if (attempt == 1 if fast_fallback else attempt <= 2)
                    else (vision or primary)
                )
            elif fast_fallback:
                natural = vision if attempt == 1 else (fallback or vision)
            else:
                # Attempts 1-2: vision model; 3+: text fallback if configured.
                natural = vision if attempt <= 2 else (fallback or vision)
            if self._is_endpoint_cooling(natural.name):
                candidates = (
                    (fallback, vision, primary)
                    if prefer_fallback
                    else (vision, fallback, primary)
                )
                for ep in candidates:
                    if ep is not None and not self._is_endpoint_cooling(ep.name):
                        return ep
            return natural

        if fallback is None:
            return primary
        if prefer_fallback:
            natural = (
                fallback
                if (attempt == 1 if fast_fallback else attempt <= 2)
                else primary
            )
        elif fast_fallback:
            natural = primary if attempt == 1 else fallback
        else:
            # Attempt 1 and 2: primary (main)
            # Attempt 3 and beyond: fallback (second provider)
            natural = primary if attempt <= 2 else fallback
        # If the chosen endpoint is rate-limit cooling and a healthy alternative
        # exists, skip straight to it. This turns a 429 on a shared upstream into
        # an immediate fallback instead of a doomed same-endpoint retry.
        # Skip the vision endpoint on text turns — it is reserved for media.
        if self._is_endpoint_cooling(natural.name):
            for ep in self._endpoints:
                if ep.name == "vision":
                    continue
                if not self._is_endpoint_cooling(ep.name):
                    return ep
        return natural

    def _is_endpoint_cooling(self, name: str) -> bool:
        expiry = self._endpoint_cooldown.get(name)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            self._endpoint_cooldown.pop(name, None)
            return False
        return True

    def _cool_endpoint(self, name: str, reason: str = "rate-limited") -> None:
        self._endpoint_cooldown[name] = time.monotonic() + self._cooldown_seconds
        logger.warning(
            "Provider endpoint %s %s; cooling for %.0fs (using alternative if available)",
            name,
            reason,
            self._cooldown_seconds,
        )

    def _should_wait_before_retry(
        self, current: ProviderEndpoint, next_endpoint: ProviderEndpoint
    ) -> bool:
        return current.name == next_endpoint.name

    def _request_payload(
        self,
        endpoint: ProviderEndpoint,
        chat_messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        disable_reasoning: bool | None = None,
    ) -> dict:
        # Model override is honored ONLY on the primary endpoint. Fallback
        # endpoints keep their configured model because the fallback is
        # selected precisely because the primary model is unhealthy. If a
        # caller passed a model override but we're routing to a fallback,
        # log a debug line so it's visible why their model was swapped.
        if model and endpoint.name != "primary" and model != endpoint.model:
            logger.debug(
                "Model override %r ignored on fallback endpoint %r (using %r)",
                model,
                endpoint.name,
                endpoint.model,
            )
        effective_model = (
            (model or endpoint.model) if endpoint.name == "primary" else endpoint.model
        )
        constraint_key = (endpoint.name, effective_model)
        effective_temperature = self.temperature if temperature is None else temperature
        # An endpoint that already rejected our temperature gets its demanded
        # value up front instead of another guaranteed 400.
        forced_temperature = self._endpoint_temperatures.get(constraint_key)
        if forced_temperature is not None:
            effective_temperature = forced_temperature
        data = {
            "model": effective_model,
            "messages": chat_messages,
            "temperature": effective_temperature,
            "stream": True,
        }
        # Always include max_tokens from config or override
        effective_max = max_tokens if max_tokens is not None else self.max_tokens
        # Proactively clamp to a previously-learned per-endpoint output cap so
        # we don't waste a round-trip re-hitting the same 400. Per-endpoint so a
        # small-cap model never lowers the cap for other endpoints.
        learned_cap = self._endpoint_output_caps.get(constraint_key)
        if learned_cap and effective_max > learned_cap:
            effective_max = learned_cap
        data["max_tokens"] = effective_max
        # Per-call disable_reasoning overrides the endpoint default; a caller
        # that passes disable_reasoning=False can keep reasoning on a shared
        # provider whose endpoint.disable_reasoning is True.
        use_disable_reasoning = (
            disable_reasoning
            if disable_reasoning is not None
            else endpoint.disable_reasoning
        )
        if use_disable_reasoning:
            # Ollama's OpenAI-compatible endpoint accepts both shapes from its
            # /v1/chat/completions docs: top-level `reasoning_effort: "none"`
            # OR nested `reasoning: {"effort": "none"}`. The literal string
            # "none" is what the docs list as a valid value (alongside "low",
            # "medium", "high", "max") — sending boolean false or
            # {"exclude": true} (OpenRouter-style) was a no-op against Ollama,
            # which is why reasoning kept streaming even with
            # disable_reasoning=True. Emit both shapes so the same payload
            # works across Ollama and OpenRouter without branching.
            data["reasoning_effort"] = "none"
            data["reasoning"] = {"effort": "none"}
            data["thinking"] = {"type": "disabled", "budget_tokens": 0}
        elif "kimi-k2.7" in str(data.get("model") or "").lower():
            # OpenCode Go's kimi-k2.7-code rejects reasoning_effort=none
            # ("invalid thinking: only type=enabled is allowed") and, if we
            # omit the thinking field, streams reasoning until max_tokens
            # with an empty content delta. Pin thinking on so the visible
            # reply actually arrives.
            data["thinking"] = {"type": "enabled"}
        if not use_disable_reasoning and (endpoint.reasoning_effort or "").strip():
            # Explicit per-endpoint effort (primary grok "low", Muse "minimal").
            data["reasoning_effort"] = endpoint.reasoning_effort.strip()
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
        if (
            data.get("stream")
            and endpoint.name not in self._endpoints_without_stream_usage
        ):
            # OpenAI/OpenRouter omit streaming usage unless asked. Without it
            # completion_tokens is 0 and TPS is guessed or missing.
            data["stream_options"] = {"include_usage": True}
        return data

    async def _get_session(self):
        async with self._session_lock:
            if self._session is None or self._session.closed:
                # BUG FIX: do NOT use SSRF-safe resolver for the provider session.
                # The default provider URL is localhost:11434 (local Ollama), and
                # the safe resolver blocks all private/loopback addresses.
                # The provider is operator-configured via env vars, not user input.
                # SSRF protection belongs on the shared session used by tools like
                # fetch_url, which DO accept untrusted URLs.
                connector = aiohttp.TCPConnector(
                    limit=16,
                    limit_per_host=6,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                    keepalive_timeout=30,
                    resolver=(
                        _PublicOnlyResolver()
                        if self.policy.public_network_only
                        else None
                    ),
                )
                self._session = aiohttp.ClientSession(
                    connector=connector, timeout=aiohttp.ClientTimeout(total=None)
                )
            return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def initialize(self):
        session = await self._get_session()
        initialized = False
        for endpoint in self._endpoints:
            try:
                async with session.get(
                    f"{endpoint.base_url}/models",
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers=await self._auth_headers(endpoint),
                    allow_redirects=self.policy.allow_redirects,
                ) as resp:
                    if resp.status == 200:
                        initialized = True
                        logger.info(
                            f"Provider endpoint initialized: {endpoint.name} ({endpoint.model})"
                        )
                    else:
                        logger.warning(
                            f"Provider endpoint {endpoint.name} /models returned {resp.status}"
                        )
            except Exception as e:
                logger.error(
                    f"Provider endpoint {endpoint.name} initialization failed: {self._display_error(e)}"
                )
        self.available = initialized
        return initialized

    @_bounded_provider_request
    async def generate_response(
        self,
        messages: list[dict],
        images: list[str] | None = None,
        media: list[dict] | None = None,
        timeout: int = 3600,
        on_tool_call_name=None,
        on_token=None,
        custom_tool_calls: bool = False,
        prefer_fallback: bool = False,
        request_id: str = "",
        **kwargs,
    ) -> ProviderResult:
        """Generate response. images is legacy b64 list, media is list of {b64, mime_type}.

        Native tool calls, usage, model and timing belong to the returned
        ProviderResult. Deprecated _last_* attributes are diagnostics only.

        If ``on_tool_call_name`` is provided, it's forwarded to the streaming
        layer so the caller gets a callback the moment a tool call name arrives
        mid-stream — useful for updating a live progress message during long
        generations (e.g. create_site where the model spends 20+ seconds
        generating HTML in the tool arguments).
        """
        tools = kwargs.get("tools")
        try:
            message = await self.generate_chat_completion(
                messages,
                images=images,
                media=media,
                timeout=timeout,
                on_tool_call_name=on_tool_call_name,
                on_token=on_token,
                custom_tool_calls=custom_tool_calls,
                prefer_fallback=prefer_fallback,
                request_id=request_id,
                **kwargs,
            )
        except RuntimeError as e:
            # Some endpoints reject tools/function calling with 400. Fall back to
            # a plain completion so XML tool tags still work.
            err = str(e).lower()
            if tools and (
                "tool" in err
                or "function" in err
                or "tools is not supported" in err
                or "does not support" in err
            ):
                logger.warning(
                    "Provider rejected native tools; retrying without tools: %s", e
                )
                kwargs = dict(kwargs)
                kwargs.pop("tools", None)
                message = await self.generate_chat_completion(
                    messages,
                    images=images,
                    media=media,
                    on_tool_call_name=on_tool_call_name,
                    on_token=on_token,
                    custom_tool_calls=custom_tool_calls,
                    timeout=timeout,
                    prefer_fallback=prefer_fallback,
                    request_id=request_id,
                    **kwargs,
                )
            else:
                raise

        tool_calls = message.get("tool_calls") or []
        tool_calls = tool_calls if isinstance(tool_calls, list) else []
        # Response cleanup may yield after generation, so shared _last_usage
        # can already belong to another request by the time this await returns.
        usage = dict(getattr(message, "usage", {}) or {})
        # Keep the shared stash for backward-compat callers / tests, but callers
        # should prefer the ProviderResult attributes (race-free).
        self._last_tool_calls = tool_calls
        self._last_assistant_message = message
        content = message.get("content") or ""
        # Multimodal / some providers return content as a list of parts
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            content = "".join(parts)
        content = content if isinstance(content, str) else str(content or "")
        if not content and not tool_calls:
            raise ProviderEmptyResponseError("Empty response from provider")
        return ProviderResult(
            content,
            tool_calls=tool_calls,
            usage=usage,
            assistant_message=message,
            model=getattr(message, "model", None),
            provider=getattr(message, "provider", self.name),
            timing=getattr(message, "timing", {}),
        )

    async def _read_completion_response(
        self,
        resp,
        *,
        stream: bool,
        byok_request: bool,
        credential_secrets: tuple[str, ...],
        on_tool_call_name=None,
        on_token=None,
        custom_tool_calls=False,
    ) -> CompletionResponse:
        """Read one successful response within its policy, preserving clocks."""
        first_token_s = last_token_s = None
        bounded = byok_request or self.policy.max_response_bytes is not None
        if stream:
            merged = await _read_sse_response(
                resp,
                on_tool_call_name=on_tool_call_name,
                on_token=on_token,
                custom_tool_calls=custom_tool_calls,
                max_bytes=self._response_limit() if bounded else None,
            )
            ended_at = time.perf_counter()
            result = {
                key: value for key, value in merged.items() if not key.startswith("__")
            }
            first_token_s = merged.get("__first_token_s__")
            last_token_s = merged.get("__last_token_s__")
        else:
            result = (
                await _read_json_response_limited(resp, self._response_limit())
                if bounded
                else await resp.json()
            )
            ended_at = time.perf_counter()
        if byok_request:
            result = self._redact_provider_payload(result, credential_secrets)
        return CompletionResponse(result, ended_at, first_token_s, last_token_s)

    def _completion_result(
        self,
        message: dict,
        response: CompletionResponse,
        *,
        endpoint: ProviderEndpoint,
        model: str,
        stream: bool,
        request_start: float,
        headers_ms: float,
        request_id: str,
        status: int,
    ) -> _CompletionMessage:
        """Attach per-request metadata and update compatibility diagnostics."""
        usage = _normalize_llm_usage(response.payload.get("usage", {}))
        request_usage = {
            key: usage[key]
            for key in (
                "prompt_tokens", "completion_tokens", "total_tokens",
                "cached_tokens", "cache_write_tokens",
            )
            if key in usage
        }
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        provider = endpoint.name if len(self._endpoints) > 1 else self.name
        timing = compute_llm_timing(
            request_start=request_start,
            first_token_s=response.first_token_s,
            last_token_s=response.last_token_s,
            ended_at=response.ended_at,
            headers_ms=headers_ms,
            usage=request_usage,
            endpoint=provider,
            model=model,
            stream=stream,
            content_chars=len(content),
            tool_calls=len(tool_calls),
            content=content,
            reasoning=message.get("reasoning_content")
            or message.get("reasoning")
            or "",
            tool_call_payloads=tool_calls,
        )
        self._last_usage = request_usage  # deprecated diagnostics only
        self._last_timing = timing  # deprecated diagnostics only
        self._timing_history.append(timing)
        self._endpoint_cooldown.pop(endpoint.name, None)
        logger.info(
            "Provider timing done request_id=%s endpoint=%s model=%s status=%s headers_ms=%.1f ttft_ms=%.1f total_ms=%.1f tps=%s content_chars=%s tool_calls=%s tokens=%s cached_tokens=%s cache_write_tokens=%s",
            request_id,
            endpoint.name,
            model,
            status,
            headers_ms,
            timing["ttft_ms"],
            timing["total_ms"],
            timing["tps"],
            len(content),
            len(tool_calls),
            request_usage["total_tokens"],
            request_usage.get("cached_tokens", "unknown"),
            request_usage.get("cache_write_tokens", "unknown"),
        )
        return _CompletionMessage(
            message, usage, model=model, provider=provider, timing=timing
        )

    @_bounded_provider_request
    async def generate_chat_completion(
        self,
        messages: list[dict],
        images: list[str] | None = None,
        media: list[dict] | None = None,
        tools: list[dict] | None = None,
        model: str | None = None,
        timeout: int = 3600,
        max_tokens: int | None = None,
        temperature: float | None = None,
        disable_reasoning: bool | None = None,
        fast_fallback: bool = False,
        on_tool_call_name=None,
        on_token=None,
        custom_tool_calls: bool = False,
        prefer_fallback: bool = False,
        request_id: str = "",
        retry_attempts: int | None = None,
        empty_response_retries: int | None = None,
        stream: bool | None = None,
        allow_media_degrade: bool = True,
    ) -> dict:
        """Generate an OpenAI-compatible assistant message, optionally with tools.

        If ``on_tool_call_name`` is provided, it's called (fire-and-forget) the
        first time a tool_call delta with a function name arrives in the SSE
        stream. This lets callers update a live progress message mid-generation.
        """
        retry_budget = (
            self.retry_attempts
            if retry_attempts is None
            else max(1, min(retry_attempts, 10))
        )
        empty_budget = (
            self.empty_response_retries
            if empty_response_retries is None
            else max(0, min(empty_response_retries, 5))
        )
        byok_request = bool(self.policy.sensitive_credentials)
        if self.policy.max_request_seconds is not None:
            timeout = min(timeout, self.policy.max_request_seconds)
        if self.policy.max_output_tokens is not None:
            max_tokens = min(
                max_tokens or self.max_tokens, self.policy.max_output_tokens
            )
        if byok_request:
            try:
                timeout = max(1, min(int(timeout), 300))
            except (TypeError, ValueError):
                timeout = 120
            try:
                max_tokens = max(
                    1, min(int(max_tokens or self.max_tokens or 4096), 4096)
                )
            except (TypeError, ValueError):
                max_tokens = 4096
        if not self.available:
            logger.warning("Provider marked unavailable; retrying initialization")
            await self.initialize()
            if not self.available:
                raise ProviderUnavailableError("Provider not available")

        chat_messages = copy.deepcopy(messages)

        all_media = []
        if media:
            all_media.extend(media)
        if images:
            all_media.extend(
                {"b64": img_b64, "mime_type": "image/png"} for img_b64 in images
            )

        payload_media: list[dict] = [
            m
            for m in all_media
            if m.get("b64")
            and (
                str(m.get("mime_type", "")).startswith(("image/", "video/"))
                or (
                    str(m.get("mime_type", "")).startswith("audio/")
                    and getattr(self, "enable_audio_input", False)
                )
            )
        ]

        if payload_media:
            target = None
            for msg in reversed(chat_messages):
                content = msg.get("content") or ""
                if isinstance(content, list):
                    content = "\n".join(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                if msg["role"] == "user" and (
                    "[User attached image" in content
                    or "[User attached media" in content
                    or "Media available to inspect" in content
                    or "Audio/video available to inspect" in content
                    or "Images available to inspect" in content
                    or "Server emoji/sticker reference sheet" in content
                ):
                    target = msg
                    break
            if target is None:
                for msg in reversed(chat_messages):
                    if msg["role"] == "user":
                        target = msg
                        break
            if target is not None:
                existing_content = target.get("content") or ""
                parts = (
                    list(existing_content)
                    if isinstance(existing_content, list)
                    else [{"type": "text", "text": existing_content}]
                )
                attached = 0
                for m in payload_media:
                    mime = m["mime_type"]
                    b64 = m["b64"]
                    uri = f"data:{mime};base64,{b64}"
                    if mime.startswith("image/"):
                        parts.append({"type": "image_url", "image_url": {"url": uri}})
                    elif mime.startswith("audio/") and getattr(
                        self, "enable_audio_input", False
                    ):
                        audio_format = AUDIO_FORMATS.get(
                            mime.split(";", 1)[0].lower(), "wav"
                        )
                        parts.append(
                            {
                                "type": "input_audio",
                                "input_audio": {"data": b64, "format": audio_format},
                            }
                        )
                    elif mime.startswith("video/"):
                        # OpenCode Go / DeepSeek reject video_url ("unknown
                        # variant"). Thumbnails and ffmpeg JPEG frames still
                        # attach as image_url.
                        logger.info(
                            "Skipping video_url part (%s); sending image frames/thumbnails only",
                            mime,
                        )
                        continue
                    else:
                        continue
                    attached += 1
                target["content"] = parts
                logger.info(f"Attached {attached} multimodal item(s) to message")
            else:
                logger.warning(
                    f"No user message found to attach {len(payload_media)} multimodal item(s)"
                )

        session = await self._get_session()
        last_error = None
        last_usage_error = None
        has_media = any(
            isinstance(part, dict)
            and part.get("type") in {"image_url", "input_audio", "video_url"}
            for msg in chat_messages
            for part in (
                msg.get("content") if isinstance(msg.get("content"), list) else []
            )
        )
        # Endpoints that rejected this call's media (text-only models 400 on
        # image_url; OpenRouter 404s on input audio). Steer retries away so a
        # GIF never dies on DeepSeek then Ling.
        media_broken: set[str] = set()
        # Endpoints that returned a deterministic non-2xx (bad model slug,
        # unsupported params). Retrying them with the same payload just repeats
        # the error, so they're excluded from the rest of this call.
        dead: set[str] = set()
        max_attempts = (
            min(retry_budget, 2)
            if fast_fallback and len(self._endpoints) > 1
            else retry_budget
        )
        # NOT a `for attempt in range(1, max_attempts + 1)`: several branches
        # below extend `max_attempts` mid-flight so a deterministic 4xx can be
        # handed to another endpoint (media-unsupported re-route, text-only
        # retry after stripping attachments, learned temperature resend,
        # generic non-2xx failover). range() snapshots its bounds at loop
        # entry, so every one of those extensions was a no-op: if the failure
        # landed on the final attempt the loop just fell out and the turn died
        # with "Provider call failed after retries" — exactly the failover the
        # extension was written to perform. A while loop re-reads the bound.
        # `attempt_ceiling` keeps a pathological provider (one that answers
        # every payload with a fresh deterministic 400) from looping forever.
        attempt = 0
        empty_response_recoveries = 0
        # Leave room for the explicitly configured empty-response recoveries in
        # addition to the bounded deterministic failover extensions below.
        attempt_ceiling = max_attempts + 2 * len(self._endpoints) + 2 + empty_budget
        recovery_endpoint: ProviderEndpoint | None = None
        context_output_limits: dict[tuple[str, str], int] = {}
        while attempt < min(max_attempts, attempt_ceiling):
            attempt += 1
            if recovery_endpoint is not None:
                # The normal schedule intentionally spends its first attempts
                # on the primary. Once it is exhausted, rotate explicitly so
                # a blank fallback response does not leave the primary unused.
                endpoint = recovery_endpoint
                recovery_endpoint = None
            else:
                endpoint = self._attempt_endpoint(
                    attempt,
                    fast_fallback=fast_fallback,
                    has_media=has_media,
                    prefer_fallback=prefer_fallback,
                )
            if endpoint.name in media_broken or endpoint.name in dead:
                order = self._media_endpoint_order() if has_media else self._endpoints
                usable = [
                    e
                    for e in order
                    if e.name not in media_broken and e.name not in dead
                ]
                if usable:
                    endpoint = usable[0]
            data = self._request_payload(
                endpoint,
                chat_messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                disable_reasoning=disable_reasoning,
            )
            if stream is not None:
                data["stream"] = bool(stream)
                if not stream:
                    data.pop("stream_options", None)
            context_limit = context_output_limits.get((endpoint.name, data["model"]))
            if context_limit is not None:
                data["max_tokens"] = min(data["max_tokens"], context_limit)
            if byok_request:
                # Avoid showing any response fragment in progress UI before
                # the complete body has passed the credential scrubber.
                data["stream"] = False
                data.pop("stream_options", None)
            if empty_response_recoveries:
                # A blank streamed 200 can be caused by a flaky SSE gateway
                # even when the provider is healthy. Use a normal JSON response
                # for recovery so the stream assembler is no longer part of the
                # failure path. Keep the caller's reasoning preference intact:
                # some models reject an explicit reasoning-disabled parameter.
                data["stream"] = False
                data.pop("stream_options", None)
            request_start = time.perf_counter()
            media_parts = sum(
                1
                for msg in chat_messages
                for part in (
                    msg.get("content") if isinstance(msg.get("content"), list) else []
                )
                if isinstance(part, dict) and part.get("type") != "text"
            )
            logger.info(
                "Provider timing start request_id=%s endpoint=%s model=%s attempt=%s/%s messages=%s media_parts=%s timeout=%s max_tokens=%s reasoning_disabled=%s tools=%s",
                request_id,
                endpoint.name,
                data.get("model"),
                attempt,
                max_attempts,
                len(chat_messages),
                media_parts,
                timeout,
                data.get("max_tokens"),
                data.get("reasoning_effort") == "none"
                or (
                    isinstance(data.get("thinking"), dict)
                    and data["thinking"].get("type") == "disabled"
                ),
                len(data.get("tools") or []),
            )
            credential_secrets: tuple[str, ...] = ()
            try:
                headers = await self._auth_headers(endpoint)
                credential_secrets = tuple(
                    value.removeprefix("Bearer ") for value in headers.values() if value
                )
                async with session.post(
                    f"{endpoint.base_url}/chat/completions",
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=timeout, connect=10),
                    headers=headers,
                    allow_redirects=self.policy.allow_redirects,
                ) as resp:
                    headers_ms = (time.perf_counter() - request_start) * 1000
                    if resp.status == 503:
                        raw_error_text = await _read_response_text_limited(resp)
                        error_text = self._display_error(
                            raw_error_text, credential_secrets
                        )
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                        )
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} 503",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                            prefer_fallback=prefer_fallback,
                        ):
                            continue
                        raise ProviderUnavailableError(
                            f"Provider overloaded after retries: {error_text[:200]}"
                        )
                    if resp.status == 429:
                        raw_error_text = await _read_response_text_limited(resp)
                        error_text = self._display_error(
                            raw_error_text, credential_secrets
                        )
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                        )
                        self._cool_endpoint(endpoint.name)
                        if _is_usage_exhausted_error(resp.status, raw_error_text):
                            last_usage_error = ProviderUsageExhaustedError(
                                f"Provider {endpoint.name} usage exhausted: {error_text[:200]}"
                            )
                            if len(self._endpoints) == 1:
                                raise last_usage_error
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} usage exhausted",
                                max_attempts=max_attempts,
                                fast_fallback=fast_fallback,
                                has_media=has_media,
                                prefer_fallback=prefer_fallback,
                            ):
                                continue
                            raise last_usage_error
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} 429 rate limited",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                            prefer_fallback=prefer_fallback,
                        ):
                            continue
                        raise ProviderRateLimitError(
                            f"Provider rate limited after retries: {error_text[:200]}"
                        )
                    if resp.status != 200:
                        raw_error_text = await _read_response_text_limited(resp)
                        error_text = self._display_error(
                            raw_error_text, credential_secrets
                        )
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s body=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                            error_text[:200],
                        )
                        if (
                            has_media
                            and not allow_media_degrade
                            and _is_media_unsupported_error(resp.status, error_text)
                        ):
                            raise ProviderMediaUnsupportedError(
                                "Provider cannot accept this media"
                            )
                        # Text-only models 400 on image_url/video_url; some
                        # fallbacks 404 on input audio. Mark broken and retry a
                        # media-capable endpoint (typically vision / primary).
                        if has_media and _is_media_unsupported_error(
                            resp.status, error_text
                        ):
                            media_broken.add(endpoint.name)
                            # Model capability, not a transient fault — remember
                            # it so later turns skip this endpoint for media.
                            self._media_incapable.add(endpoint.name)
                            order = self._media_endpoint_order()
                            usable = [e for e in order if e.name not in media_broken]
                            if not usable:
                                # Every endpoint refused the attachments. Drop
                                # them and answer the text instead of failing
                                # the whole turn.
                                if _strip_media_parts(chat_messages):
                                    logger.warning(
                                        "No endpoint accepts this media; retrying text-only: %s",
                                        error_text[:200],
                                    )
                                    has_media = False
                                    media_broken.clear()
                                    max_attempts = max(
                                        max_attempts, attempt + len(self._endpoints)
                                    )
                                    continue
                                raise RuntimeError(
                                    f"Provider {endpoint.name} media-unsupported and no alternatives: {error_text[:200]}"
                                )
                            if attempt >= max_attempts:
                                max_attempts = attempt + len(usable)
                            logger.warning(
                                "Provider endpoint %s cannot handle media; retrying with %s",
                                endpoint.name,
                                usable[0].name,
                            )
                            continue
                        # Provider-side function degradation (e.g. "DEGRADED function
                        # cannot be invoked"). This is NOT transient — don't waste
                        # retries on the same endpoint; cool it and fall back now.
                        if resp.status == 400 and "degraded" in error_text.lower():
                            self._cool_endpoint(endpoint.name)
                            logger.warning(
                                "Provider endpoint %s marked degraded; skipping to fallback",
                                endpoint.name,
                            )
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} degraded",
                                max_attempts=max_attempts,
                                fast_fallback=fast_fallback,
                                has_media=has_media,
                                prefer_fallback=prefer_fallback,
                            ):
                                continue
                            raise ProviderUnavailableError(
                                f"Provider {endpoint.name} degraded and no fallback available: {error_text[:200]}",
                                cooldown=True,
                            )
                        # Region / geo blocks (DeepSeek V4 Flash China opt-in)
                        # are not transient. Don't burn a 2s retry on the same
                        # endpoint — cool it and fail over immediately.
                        if resp.status == 403 or "regionerror" in error_text.lower():
                            self._cool_endpoint(endpoint.name)
                            logger.warning(
                                "Provider endpoint %s returned 403/region block; skipping to fallback",
                                endpoint.name,
                            )
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} 403",
                                max_attempts=max_attempts,
                                fast_fallback=True,
                                has_media=has_media,
                                prefer_fallback=prefer_fallback,
                            ):
                                continue
                            raise ProviderAuthenticationError(
                                f"Provider {endpoint.name} 403 and no fallback available: {error_text[:200]}"
                            )
                        # Content-policy prompt blocks (Gemini "sensitive words
                        # that violate Google's use policy"). Not transient and
                        # not payload-shaped: the same text will be refused every
                        # time, so cool the endpoint and hand the turn to the
                        # fallback model rather than burning retries or surfacing
                        # a raw Google error into the channel.
                        if _is_content_policy_block(resp.status, error_text):
                            self._cool_endpoint(
                                endpoint.name, "blocked the prompt on content policy"
                            )
                            logger.warning(
                                "Provider endpoint %s blocked the prompt on content policy; "
                                "failing over: %s",
                                endpoint.name,
                                error_text[:200],
                            )
                            if await self._retry_after_attempt(
                                attempt,
                                endpoint,
                                f"Provider {endpoint.name} content-policy block",
                                max_attempts=max_attempts,
                                fast_fallback=True,
                                has_media=has_media,
                                prefer_fallback=prefer_fallback,
                            ):
                                continue
                            raise ProviderUnavailableError(
                                f"Provider {endpoint.name} blocked this prompt on content "
                                f"policy and no fallback endpoint was available",
                                cooldown=True,
                            )
                        current_output = int(data.get("max_tokens", self.max_tokens))
                        safe_context_output = (
                            context_output_limit(
                                resp.status, error_text, current_output
                            )
                            if max_tokens is None
                            else None
                        )
                        if safe_context_output is not None:
                            logger.warning(
                                "Clamping max_tokens from %s to %s for endpoint %s after context overflow",
                                current_output,
                                safe_context_output,
                                endpoint.name,
                            )
                            context_output_limits[(endpoint.name, data["model"])] = (
                                safe_context_output
                            )
                            if attempt >= max_attempts:
                                max_attempts = attempt + 1
                            recovery_endpoint = endpoint
                            continue
                        safe_output = maximum_output_limit(
                            resp.status, error_text, current_output
                        )
                        if safe_output is not None:
                            logger.warning(
                                "Clamping max_tokens from %s to %s for endpoint %s model %s",
                                current_output,
                                safe_output,
                                endpoint.name,
                                data["model"],
                            )
                            self._endpoint_output_caps[
                                (endpoint.name, data["model"])
                            ] = safe_output
                            if attempt >= max_attempts:
                                max_attempts = attempt + 1
                            recovery_endpoint = endpoint
                            continue
                        # Some models accept exactly one temperature and 400 on
                        # anything else. Learn it and resend to the SAME endpoint
                        # rather than burning retries / falling back needlessly.
                        if _is_stream_options_rejected(
                            resp.status, error_text
                        ) and data.get("stream_options"):
                            self._endpoints_without_stream_usage.add(endpoint.name)
                            logger.warning(
                                "Provider endpoint %s rejected stream_options; "
                                "resending without include_usage",
                                endpoint.name,
                            )
                            if attempt >= max_attempts:
                                max_attempts = attempt + 1
                            recovery_endpoint = endpoint
                            continue
                        required_temp = _required_temperature(resp.status, error_text)
                        if (
                            required_temp is not None
                            and self._endpoint_temperatures.get(
                                (endpoint.name, data["model"])
                            )
                            != required_temp
                        ):
                            logger.warning(
                                "Provider endpoint %s requires temperature=%s; resending",
                                endpoint.name,
                                required_temp,
                            )
                            # Recorded per-endpoint only. Assigning the local
                            # `temperature` override instead would carry this
                            # endpoint's constraint onto every other endpoint
                            # this call later touches.
                            self._endpoint_temperatures[
                                (endpoint.name, data["model"])
                            ] = required_temp
                            if attempt >= max_attempts:
                                max_attempts = attempt + 1
                            recovery_endpoint = endpoint
                            continue
                        # Anything else non-2xx used to die right here with no
                        # failover, so a 404 "model unavailable for free" on the
                        # primary killed the whole turn while a healthy fallback
                        # sat unused (logged 2026-08-07). The body is
                        # deterministic, so hand the call to a *different*
                        # endpoint — repeating it here would just 404 again.
                        # No _cool_endpoint(): a 400 from our own payload would
                        # otherwise park all traffic on the fallback for a full
                        # minute. Failing over for this call is enough.
                        dead.add(endpoint.name)
                        alternatives = [
                            e for e in self._endpoints if e.name not in dead
                        ]
                        if alternatives:
                            if attempt >= max_attempts:
                                max_attempts = attempt + len(alternatives)
                            logger.warning(
                                "Provider endpoint %s returned %s; failing over to %s",
                                endpoint.name,
                                resp.status,
                                alternatives[0].name,
                            )
                            continue
                        error_class = (
                            ProviderAuthenticationError
                            if resp.status in {401, 403}
                            else ProviderInvalidRequestError
                            if resp.status == 400
                            else ProviderRequestError
                        )
                        raise error_class(
                            f"Provider API error: {resp.status} - {error_text}"
                        )

                    response = await self._read_completion_response(
                        resp,
                        stream=bool(data.get("stream")),
                        byok_request=byok_request,
                        credential_secrets=credential_secrets,
                        on_tool_call_name=on_tool_call_name,
                        on_token=on_token,
                        custom_tool_calls=custom_tool_calls,
                    )
                    result = response.payload
                    if not isinstance(result, dict):
                        result_preview = (
                            "[redacted]"
                            if byok_request
                            else (str(result)[:600] if result is not None else "None")
                        )
                        logger.warning(
                            "Provider %s returned 200 with non-dict JSON body (type=%s) preview=%s",
                            endpoint.name,
                            type(result).__name__,
                            result_preview,
                        )
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} returned non-dict JSON body",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                            prefer_fallback=prefer_fallback,
                        ):
                            continue
                        raise RuntimeError(
                            "No response from provider (non-dict JSON body)"
                        )
                    choices = result.get("choices", [])
                    if not choices:
                        # Log details to debug providers that return 200 OK with empty choices
                        # (common with some models/endpoints on safety, overload, or format quirks).
                        result_keys = (
                            list(result.keys())
                            if isinstance(result, dict)
                            else type(result).__name__
                        )
                        result_preview = (
                            "[redacted]"
                            if byok_request
                            else (str(result)[:600] if result else "")
                        )
                        logger.warning(
                            "Provider %s returned 200 with no choices. keys=%s preview=%s",
                            endpoint.name,
                            result_keys,
                            result_preview,
                        )
                        if isinstance(result, dict) and "error" in result:
                            err_obj = result["error"]
                            logger.warning(
                                "Provider %s also included error in body: %s",
                                endpoint.name,
                                "[redacted]" if byok_request else str(err_obj)[:300],
                            )
                            upstream_code = (
                                err_obj.get("code", "")
                                if isinstance(err_obj, dict)
                                else ""
                            )
                            raise RuntimeError(
                                f"No response from provider (upstream error code: {upstream_code})"
                                if upstream_code
                                else "No response from provider"
                            )
                        raise RuntimeError("No response from provider")

                    message = normalize_completion_message(choices)
                    content = message["content"]
                    # Reasoning-to-content promotion.
                    #
                    # Some reasoning models (notably DeepSeek) have a quirk
                    # where the *answer* genuinely rides in `reasoning_content`
                    # with `content` left null. Commit 010b0db promoted
                    # reasoning -> content unconditionally to fix that, but it
                    # was too blunt: for an interrupted/cut-off reasoning model
                    # (grok, ollama-native, minimax-m3) a null-content + only
                    # reasoning reply is usually chain-of-thought with NO
                    # answer produced — promoting it sends the scratchpad to
                    # the channel as the user-visible reply (logged leak: the
                    # bot posted "The user is making a sexual joke about
                    # 'Bobby Fisher'… I should decline" to Discord).
                    #
                    # Rule: NEVER promote when there are tool_calls (reasoning
                    # accompanying a tool call is unambiguously internal), and
                    # NEVER promote on providers whose answers always arrive
                    # in `content`. Only promote for the known DeepSeek-family
                    # case where an empty-content answer legitimately lives in
                    # reasoning_content. Everything else drops through to the
                    # empty-response retry/fallback below instead of leaking.
                    if (
                        not content
                        and not message.get("tool_calls")
                        and self._reasoning_content_is_answer(endpoint, message)
                    ):
                        content = (
                            message.get("reasoning_content")
                            or message.get("reasoning")
                            or ""
                        )
                        if content:
                            message["content"] = content
                    # A blocked prompt comes back as a normal 200 whose content
                    # IS Google's notice. Never let that reach the channel: drop
                    # it, cool the endpoint and hand the turn to the fallback
                    # model on the very next attempt (no second try against the
                    # model that just refused — the same payload always loses).
                    if content and _is_policy_block_text(content):
                        self._cool_endpoint(
                            endpoint.name, "blocked the prompt on content policy"
                        )
                        logger.warning(
                            "Provider %s returned a content-policy prompt block as its "
                            "reply; discarding it and failing over",
                            endpoint.name,
                        )
                        content = ""
                        message["content"] = ""
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} content-policy block",
                            max_attempts=max_attempts,
                            fast_fallback=True,
                            has_media=has_media,
                            prefer_fallback=prefer_fallback,
                        ):
                            continue
                        raise ProviderUnavailableError(
                            "Prompt was blocked by the provider's content policy and "
                            "no fallback endpoint was available",
                            cooldown=True,
                        )
                    if not content and not message.get("tool_calls"):
                        # Some providers return choices with a message but blank content (e.g. refusals, reasoning-only, or bugs).
                        logger.warning(
                            "Provider %s returned 200 with empty content (tool_calls=%s) message_keys=%s",
                            endpoint.name,
                            bool(message.get("tool_calls")),
                            list(message.keys())
                            if isinstance(message, dict)
                            else type(message).__name__,
                        )
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} returned empty response",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                            has_media=has_media,
                            prefer_fallback=prefer_fallback,
                        ):
                            continue
                        if empty_response_recoveries < empty_budget:
                            empty_response_recoveries += 1
                            max_attempts = min(
                                attempt_ceiling, max(max_attempts, attempt + 1)
                            )
                            order = (
                                self._media_endpoint_order()
                                if has_media
                                else [e for e in self._endpoints if e.name != "vision"]
                            )
                            usable = [
                                e
                                for e in order
                                if e.name not in media_broken and e.name not in dead
                            ]
                            alternatives = [
                                e for e in usable if e.name != endpoint.name
                            ]
                            if alternatives:
                                healthy = [
                                    e
                                    for e in alternatives
                                    if not self._is_endpoint_cooling(e.name)
                                ]
                                recovery_endpoint = (healthy or alternatives)[0]
                            else:
                                recovery_endpoint = endpoint
                            logger.warning(
                                "Provider %s returned an empty response after the "
                                "normal retry budget; recovery %s/%s using %s "
                                "(non-streaming)",
                                endpoint.name,
                                empty_response_recoveries,
                                self.empty_response_retries,
                                recovery_endpoint.name,
                            )
                            if recovery_endpoint.name == endpoint.name:
                                await asyncio.sleep(
                                    min(2 * empty_response_recoveries, 6)
                                )
                            continue
                        raise ProviderEmptyResponseError("Empty response from provider")

                    return self._completion_result(
                        message,
                        response,
                        endpoint=endpoint,
                        model=str(data.get("model") or ""),
                        stream=bool(data.get("stream")),
                        request_start=request_start,
                        headers_ms=headers_ms,
                        request_id=request_id,
                        status=resp.status,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "Provider timing timeout request_id=%s endpoint=%s elapsed_ms=%.1f timeout=%s",
                    request_id,
                    endpoint.name,
                    (time.perf_counter() - request_start) * 1000,
                    timeout,
                )
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} timeout",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                    has_media=has_media,
                    prefer_fallback=prefer_fallback,
                ):
                    continue
                raise ProviderUnavailableError(
                    f"Provider request timed out after {timeout}s"
                ) from asyncio.TimeoutError
            except ProviderUsageExhaustedError:
                raise
            except ProviderRequestError:
                # Deterministic and already failed over everywhere it could.
                raise
            except RuntimeError as e:
                last_error = e
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} error: {self._display_error(e, credential_secrets)}",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                    has_media=has_media,
                    prefer_fallback=prefer_fallback,
                ):
                    continue
                if isinstance(e, ProviderError):
                    raise
                raise ProviderUnavailableError(
                    self._display_error(e, credential_secrets)
                ) from None
            except Exception as e:
                last_error = e
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} error: {self._display_error(e, credential_secrets)}",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                    has_media=has_media,
                    prefer_fallback=prefer_fallback,
                ):
                    continue
                raise ProviderUnavailableError(
                    f"Provider call failed: {self._display_error(last_error, credential_secrets)}"
                ) from None
        if last_usage_error:
            raise last_usage_error
        raise ProviderUnavailableError("Provider call failed after retries")

    async def _retry_after_attempt(
        self,
        attempt: int,
        endpoint: ProviderEndpoint,
        reason: str,
        *,
        max_attempts: int | None = None,
        fast_fallback: bool = False,
        has_media: bool = False,
        prefer_fallback: bool = False,
    ) -> bool:
        max_attempts = max_attempts or self.retry_attempts
        if attempt >= max_attempts:
            return False
        next_endpoint = self._attempt_endpoint(
            attempt + 1,
            fast_fallback=fast_fallback,
            has_media=has_media,
            prefer_fallback=prefer_fallback,
        )
        if self._should_wait_before_retry(endpoint, next_endpoint):
            base = min(8, attempt * 1.5)
            jitter = _random.uniform(0, 0.7)
            wait = round(base + jitter, 2)
            logger.warning(
                f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying in {wait}s..."
            )
            await asyncio.sleep(wait)
        else:
            logger.warning(
                f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying with {next_endpoint.name} provider..."
            )
            await asyncio.sleep(_random.uniform(0.05, 0.25))
        return True


# Deprecated import alias. New integrations must use OpenAICompatibleProvider.
OllamaProvider = OpenAICompatibleProvider
