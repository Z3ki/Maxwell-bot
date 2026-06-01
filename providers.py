"""Ollama AI Provider for Maxwell Bot"""

import asyncio
import aiohttp
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

USAGE_EXHAUSTED_MESSAGE = "The api is down cuz yall drained the usage and im not rich so wait like 2 hours"

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
    ".mp4": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


class ProviderUsageExhaustedError(RuntimeError):
    """Raised when the upstream provider is out of quota, credits, or cooldown capacity."""

    user_message = USAGE_EXHAUSTED_MESSAGE


def _is_usage_exhausted_error(status: int, error_text: str) -> bool:
    text = (error_text or "").lower()
    markers = (
        "model_cooldown",
        "cooling down",
        "quota",
        "insufficient_quota",
        "insufficient credits",
        "credit balance",
        "usage",
        "rate limit",
        "rate_limit",
    )
    return status == 429 and any(marker in text for marker in markers)


@dataclass(frozen=True)
class ProviderEndpoint:
    name: str
    base_url: str
    model: str
    api_key: str = ""
    disable_reasoning: bool = False


class OllamaProvider:
    """OpenAI-compatible LLM Provider with multimodal support using /v1/chat/completions"""

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
        retry_attempts: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = api_key.strip()
        self.retry_attempts = max(1, retry_attempts)
        self._endpoints = [
            ProviderEndpoint("primary", self.base_url, self.model, self.api_key, disable_reasoning),
        ]
        if fallback_base_url and fallback_model:
            self._endpoints.append(
                ProviderEndpoint(
                    "fallback",
                    fallback_base_url.rstrip("/"),
                    fallback_model,
                    fallback_api_key.strip(),
                    fallback_disable_reasoning,
                )
            )
        self._session = None
        self.available = False
        self._last_usage: dict = {}

    def _headers(self, endpoint: ProviderEndpoint = None) -> dict[str, str]:
        api_key = self.api_key if endpoint is None else endpoint.api_key
        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    def _attempt_endpoint(self, attempt: int, *, fast_fallback: bool = False) -> ProviderEndpoint:
        if len(self._endpoints) < 2:
            return self._endpoints[0]
        if fast_fallback:
            return self._endpoints[0] if attempt == 1 else self._endpoints[1]
        # Attempt 1 and 2: primary (main)
        # Attempt 3 and beyond: fallback (second provider)
        return self._endpoints[0] if attempt <= 2 else self._endpoints[1]

    def _should_wait_before_retry(self, current: ProviderEndpoint, next_endpoint: ProviderEndpoint) -> bool:
        return current.name == next_endpoint.name

    def _request_payload(
        self,
        endpoint: ProviderEndpoint,
        chat_messages: list[dict],
        tools: list[dict] = None,
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        disable_reasoning: bool = None,
    ) -> dict:
        data = {
            "model": (model or endpoint.model) if endpoint.name == "primary" else endpoint.model,
            "messages": chat_messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }
        # Always include max_tokens from config or override
        data["max_tokens"] = max_tokens if max_tokens is not None else self.max_tokens
        if endpoint.disable_reasoning or disable_reasoning:
            data["reasoning"] = {"exclude": True}
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
        return data

    async def _get_session(self):
        if self._session is None or self._session.closed:
            # BUG FIX: do NOT use SSRF-safe resolver for the provider session.
            # The default provider URL is localhost:11434 (local Ollama), and
            # the safe resolver blocks all private/loopback addresses.
            # The provider is operator-configured via env vars, not user input.
            # SSRF protection belongs on the shared session used by tools like
            # fetch_url, which DO accept untrusted URLs.
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=3)
            self._session = aiohttp.ClientSession(connector=connector)
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
                    headers=self._headers(endpoint),
                ) as resp:
                    if resp.status == 200:
                        initialized = True
                        logger.info(f"Provider endpoint initialized: {endpoint.name} ({endpoint.model})")
                    else:
                        logger.warning(f"Provider endpoint {endpoint.name} /models returned {resp.status}")
            except Exception as e:
                logger.error(f"Provider endpoint {endpoint.name} initialization failed: {e}")
        self.available = initialized
        return initialized

    async def generate_response(
        self, messages: list[dict], images: list[str] = None, media: list[dict] = None, timeout: int = 60, **kwargs
    ) -> str:
        """Generate response. images is legacy b64 list, media is list of {b64, mime_type}."""
        message = await self.generate_chat_completion(messages, images=images, media=media, timeout=timeout, **kwargs)
        if message.get("tool_calls"):
            # This bot intentionally uses XML-ish text tool tags. Native provider
            # tool_calls need a totally different message loop; pretending they are
            # text is how tool orchestration gets cursed.
            raise RuntimeError("Native provider tool_calls are not supported in generate_response; use XML tool tags")
        content = message.get("content", "")
        if not content:
            raise RuntimeError("Empty response from provider")
        return content

    async def generate_chat_completion(
        self,
        messages: list[dict],
        images: list[str] = None,
        media: list[dict] = None,
        tools: list[dict] = None,
        model: str = None,
        timeout: int = 60,
        max_tokens: int = None,
        temperature: float = None,
        disable_reasoning: bool = None,
        fast_fallback: bool = False,
    ) -> dict:
        """Generate an OpenAI-compatible assistant message, optionally with tools."""
        if not self.available:
            raise RuntimeError("Provider not available")

        chat_messages = [dict(m) for m in messages]

        all_media = []
        if media:
            all_media.extend(media)
        if images:
            for img_b64 in images:
                all_media.append({"b64": img_b64, "mime_type": "image/png"})

        payload_media = []
        for m in all_media:
            mime = str(m.get("mime_type", ""))
            if m.get("b64") and mime.startswith(("image/", "audio/", "video/")):
                payload_media.append(m)

        if payload_media:
            target = None
            for msg in chat_messages:
                content = msg.get("content", "")
                if msg["role"] == "user" and (
                    "[User attached image" in content
                    or "[User attached media" in content
                    or "Media available to inspect" in content
                    or "Audio/video available to inspect" in content
                    or "Images available to inspect" in content
                ):
                    target = msg
                    break
            if target is None:
                for msg in reversed(chat_messages):
                    if msg["role"] == "user":
                        target = msg
                        break
            if target is not None:
                parts = [{"type": "text", "text": target.get("content", "")}]
                attached = 0
                for m in payload_media:
                    mime = m["mime_type"]
                    b64 = m["b64"]
                    uri = f"data:{mime};base64,{b64}"
                    if mime.startswith("image/"):
                        parts.append({"type": "image_url", "image_url": {"url": uri}})
                    elif mime.startswith("audio/"):
                        audio_format = AUDIO_FORMATS.get(mime.split(";", 1)[0].lower(), "wav")
                        parts.append({"type": "input_audio", "input_audio": {"data": b64, "format": audio_format}})
                    elif mime.startswith("video/"):
                        parts.append({"type": "video_url", "video_url": {"url": uri}})
                    else:
                        continue
                    attached += 1
                target["content"] = parts
                logger.info(f"Attached {attached} multimodal item(s) to message")
            else:
                logger.warning(f"No user message found to attach {len(payload_media)} multimodal item(s)")

        session = await self._get_session()
        last_error = None
        last_usage_error = None
        max_attempts = min(self.retry_attempts, 2) if fast_fallback and len(self._endpoints) > 1 else self.retry_attempts
        for attempt in range(1, max_attempts + 1):
            endpoint = self._attempt_endpoint(attempt, fast_fallback=fast_fallback)
            data = self._request_payload(endpoint, chat_messages, tools=tools, model=model, max_tokens=max_tokens, temperature=temperature, disable_reasoning=disable_reasoning)
            request_start = time.perf_counter()
            media_parts = sum(1 for msg in chat_messages for part in (msg.get("content") if isinstance(msg.get("content"), list) else []) if isinstance(part, dict) and part.get("type") != "text")
            logger.info(
                "Provider timing start endpoint=%s model=%s attempt=%s/%s messages=%s media_parts=%s timeout=%s max_tokens=%s reasoning_disabled=%s",
                endpoint.name,
                data.get("model"),
                attempt,
                max_attempts,
                len(chat_messages),
                media_parts,
                timeout,
                data.get("max_tokens"),
                bool(data.get("reasoning")),
            )
            try:
                async with session.post(
                    f"{endpoint.base_url}/chat/completions",
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=timeout, connect=10),
                    headers=self._headers(endpoint),
                ) as resp:
                    headers_ms = (time.perf_counter() - request_start) * 1000
                    if resp.status == 503:
                        error_text = await resp.text()
                        logger.warning("Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s", endpoint.name, resp.status, headers_ms, len(error_text))
                        if await self._retry_after_attempt(attempt, endpoint, f"Provider {endpoint.name} 503", max_attempts=max_attempts, fast_fallback=fast_fallback):
                            continue
                        raise RuntimeError(f"Provider overloaded after retries: {error_text[:200]}")
                    if resp.status == 429:
                        error_text = await resp.text()
                        logger.warning("Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s", endpoint.name, resp.status, headers_ms, len(error_text))
                        if _is_usage_exhausted_error(resp.status, error_text):
                            last_usage_error = ProviderUsageExhaustedError(
                                f"Provider {endpoint.name} usage exhausted: {error_text[:200]}"
                            )
                            if len(self._endpoints) == 1:
                                raise last_usage_error
                            if await self._retry_after_attempt(attempt, endpoint, f"Provider {endpoint.name} usage exhausted", max_attempts=max_attempts, fast_fallback=fast_fallback):
                                continue
                            raise last_usage_error
                        if await self._retry_after_attempt(attempt, endpoint, f"Provider {endpoint.name} 429 rate limited", max_attempts=max_attempts, fast_fallback=fast_fallback):
                            continue
                        raise RuntimeError(f"Provider rate limited after retries: {error_text[:200]}")
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning("Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s", endpoint.name, resp.status, headers_ms, len(error_text))
                        # Auto-clamp max_tokens on context overflow (OpenRouter returns 400)
                        if resp.status == 400 and "maximum context length" in error_text.lower() and max_tokens is None:
                            import re as _re
                            ctx_match = _re.search(r"maximum context length is (\d+) tokens", error_text)
                            req_match = _re.search(r"you requested about (\d+) tokens", error_text)
                            if ctx_match and req_match:
                                ctx_limit = int(ctx_match.group(1))
                                requested = int(req_match.group(1))
                                estimated_input = requested - int(data.get("max_tokens", self.max_tokens))
                                safe_output = max(4096, ctx_limit - estimated_input - 512)
                                if safe_output < int(data.get("max_tokens", self.max_tokens)):
                                    logger.warning("Clamping max_tokens from %s to %s due to context limit %s", data.get("max_tokens"), safe_output, ctx_limit)
                                    # The loop rebuilds payloads every attempt. Mutating only
                                    # data["max_tokens"] here is a fake fix; keep the clamp in
                                    # loop state or we retry the same busted request like idiots.
                                    max_tokens = safe_output
                                    data["max_tokens"] = safe_output
                                    if await self._retry_after_attempt(attempt, endpoint, f"Context overflow, clamped max_tokens to {safe_output}", max_attempts=max_attempts, fast_fallback=fast_fallback):
                                        continue
                        raise RuntimeError(
                            f"Provider API error: {resp.status} - {error_text}"
                        )

                    result = await resp.json()
                    json_ms = (time.perf_counter() - request_start) * 1000
                    choices = result.get("choices", [])
                    if not choices:
                        raise RuntimeError("No response from provider")

                    message = choices[0].get("message", {})
                    content = message.get("content", "")
                    if not content and not message.get("tool_calls"):
                        if await self._retry_after_attempt(attempt, endpoint, f"Provider {endpoint.name} returned empty response", max_attempts=max_attempts, fast_fallback=fast_fallback):
                            continue
                        raise RuntimeError("Empty response from provider")

                    usage = result.get("usage", {})
                    self._last_usage = {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    }
                    logger.info(
                        "Provider timing done endpoint=%s status=%s headers_ms=%.1f total_ms=%.1f content_chars=%s tool_calls=%s tokens=%s",
                        endpoint.name,
                        resp.status,
                        headers_ms,
                        json_ms,
                        len(content or ""),
                        len(message.get("tool_calls") or []),
                        self._last_usage.get("total_tokens", 0),
                    )
                    return message
            except asyncio.TimeoutError:
                logger.warning("Provider timing timeout endpoint=%s elapsed_ms=%.1f timeout=%s", endpoint.name, (time.perf_counter() - request_start) * 1000, timeout)
                if await self._retry_after_attempt(attempt, endpoint, f"Provider {endpoint.name} timeout", max_attempts=max_attempts, fast_fallback=fast_fallback):
                    continue
                raise RuntimeError(f"Provider request timed out after {timeout}s")
            except ProviderUsageExhaustedError:
                raise
            except RuntimeError as e:
                last_error = e
                if await self._retry_after_attempt(attempt, endpoint, f"Provider {endpoint.name} error: {e}", max_attempts=max_attempts, fast_fallback=fast_fallback):
                    continue
                raise
            except Exception as e:
                last_error = e
                if await self._retry_after_attempt(attempt, endpoint, f"Provider {endpoint.name} error: {e}", max_attempts=max_attempts, fast_fallback=fast_fallback):
                    continue
                raise RuntimeError(f"Provider call failed: {last_error}")
        if last_usage_error:
            raise last_usage_error
        raise RuntimeError("Provider call failed after retries")

    async def _retry_after_attempt(self, attempt: int, endpoint: ProviderEndpoint, reason: str, *, max_attempts: int = None, fast_fallback: bool = False) -> bool:
        max_attempts = max_attempts or self.retry_attempts
        if attempt >= max_attempts:
            return False
        next_endpoint = self._attempt_endpoint(attempt + 1, fast_fallback=fast_fallback)
        if self._should_wait_before_retry(endpoint, next_endpoint):
            wait = attempt * 2
            logger.warning(f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying in {wait}s...")
            await asyncio.sleep(wait)
        else:
            logger.warning(
                f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying with {next_endpoint.name} provider..."
            )
        return True
