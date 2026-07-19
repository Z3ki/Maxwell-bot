"""Ollama AI Provider for Maxwell Bot"""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# Matches the `reasoning` string value inside a (possibly partial) tool-call
# arguments JSON. Models emit reasoning as the FIRST field, well before any
# huge field like create_site's `body`, so once this regex matches the value's
# closing quote is in hand and we can surface the reasoning to the live
# progress message without waiting for the rest of the stream.
_PARTIAL_REASONING_RE = re.compile(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"')


async def _safe_call(cb, *args, **kwargs):
    """Await an SSE callback, swallowing any exception. Used for fire-and-forget
    callbacks (``asyncio.create_task(_safe_call(...))``) so a buggy callback
    never crashes the streaming read loop."""
    try:
        await cb(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.debug("SSE callback raised: %s", e)


def _extract_partial_reasoning(arguments: str) -> str:
    """Best-effort pull of the `reasoning` string from a PARTIAL arguments JSON.

    Returns '' until the reasoning value's closing quote has arrived (i.e. the
    model is still emitting it). Once complete, returns the decoded string.
    Used to update the in-channel progress message with the model's real intent
    mid-stream, instead of a static "generating…" for the whole generation.
    """
    if not arguments:
        return ""
    # Fast path: the whole arguments object already parses.
    try:
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            r = parsed.get("reasoning")
            if isinstance(r, str):
                return r
    except (json.JSONDecodeError, ValueError):
        pass
    # Partial JSON: grab the reasoning value once its closing quote landed.
    m = _PARTIAL_REASONING_RE.search(arguments)
    if not m:
        return ""
    raw = m.group(1)
    try:
        return json.loads('"' + raw + '"')  # decode \n, \", etc.
    except (json.JSONDecodeError, ValueError):
        return raw


async def _read_sse_response(
    resp: aiohttp.ClientResponse,
    on_tool_call_name=None,
    on_token=None,
) -> dict:
    """Read an OpenAI-style SSE chat-completions stream and reassemble it into
    the same dict shape a non-streamed `await resp.json()` would return.

    If ``on_tool_call_name`` is provided, it's awaited the first time a
    tool_call delta arrives with a function name. This lets the caller
    update a live progress message mid-stream — e.g. show
    "create_site: …" while the model is still generating the tool arguments
    (the HTML body), instead of waiting for the entire response to finish.

    If ``on_token`` is provided, it's called (fire-and-forget, NEVER awaited
    inline) on every content and reasoning delta so the caller can show a
    live progress message with a rolling preview of the model's own words.
    Inline awaiting would back-pressure the SSE read on a slow Discord edit
    and stall the upstream. The callback gets a small dict with the new
    delta (NOT an accumulator) plus a flag distinguishing reasoning from
    visible content::

        {"reasoning": str, "content": str, "tool_name": str|None}

    ``tool_name`` is set only on the delta that first introduces a tool call
    name (so the callback can switch the progress UI from "model is
    thinking" to "tool_name: …" the moment the model decides).

    The OpenAI streaming protocol sends one JSON object per ``data:`` line, each
    with the same frame structure but only the *delta* of what changed since
    the previous frame:

        data: {"choices": [{"delta": {"role": "assistant"}, "index": 0}]}
        data: {"choices": [{"delta": {"content": "hello"}, "index": 0}]}
        data: {"choices": [{"delta": {"content": " world"}, "index": 0}]}
        data: {"choices": [{"delta": {"tool_calls": [...]}, "index": 0}]}
        data: {"choices": [{"finish_reason": "stop", "index": 0}]}
        data: [DONE]

    We accumulate content strings, tool_calls (pinned by ``index``), and any
    usage payload that streams in at the end, then return a dict that matches
    the non-streamed response shape so the rest of the request handler does
    not need to care which mode produced the response.

    Returns the merged dict, plus (via a sentinel) the time the first content
    delta was received — encoded as ``__first_token_ms__`` in the returned
    dict and popped by the caller.

    Raises RuntimeError if the stream is malformed (no choices ever arrive) so
    the upstream retry logic can take over.
    """
    merged: dict = {"choices": [{}]}
    tool_calls_by_index: dict[int, dict] = {}
    content_parts: list[str] = []
    role: str | None = None
    finish_reason: str | None = None
    reasoning_parts: list[str] = []
    first_token_s: float | None = None
    done = False

    buf = b""
    async for raw_chunk in resp.content.iter_any():
        if done:
            break
        buf += raw_chunk
        while b"\n" in buf and not done:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            # SSE comments / non-data lines start with ":" — ignore.
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].lstrip()
            if payload == b"[DONE]":
                done = True
                break
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                # Malformed frame — skip rather than fail the whole stream.
                # Providers occasionally send keepalives or partial frames.
                continue
            if first_token_s is None:
                first_token_s = time.perf_counter()
            for choice in obj.get("choices", []) or []:
                idx = choice.get("index", 0)
                # Ensure the choices slot for this index exists.
                while len(merged["choices"]) <= idx:
                    merged["choices"].append({})
                delta = choice.get("delta") or {}
                if delta.get("role"):
                    role = delta["role"]
                if "content" in delta and delta["content"] is not None:
                    content_parts.append(delta["content"])
                # Reasoning deltas: OpenAI/DeepSeek-style models use
                # `reasoning_content`; Ollama cloud's minimax-m3 emits a
                # `reasoning` field on the same delta. Treat both the same
                # way so the bot's existing reasoning handler picks them up.
                for rkey in ("reasoning_content", "reasoning"):
                    rval = delta.get(rkey)
                    if rval is not None:
                        reasoning_parts.append(rval)
                # Per-token progress callback (fire-and-forget, NEVER awaited
                # inline). A slow Discord edit must not back-pressure the SSE
                # read — that would stall the upstream provider and add visible
                # latency to the stream. We hand the caller a small dict with
                # the NEW deltas from this frame plus an empty tool_name that
                # the tool_call block below may fill in.
                if on_token is not None:
                    tok_content = delta.get("content") or ""
                    tok_reason = ""
                    for rkey in ("reasoning_content", "reasoning"):
                        rv = delta.get(rkey)
                        if rv:
                            tok_reason = rv
                            break
                    if tok_content or tok_reason:
                        try:
                            on_token(
                                {
                                    "content": tok_content,
                                    "reasoning": tok_reason,
                                    "tool_name": None,
                                }
                            )
                        except Exception:
                            pass
                if "tool_calls" in delta and delta["tool_calls"]:
                    for tc_delta in delta["tool_calls"]:
                        tc_idx = tc_delta.get("index", 0)
                        slot = tool_calls_by_index.get(tc_idx)
                        if slot is None:
                            slot = {
                                "id": tc_delta.get("id"),
                                "type": tc_delta.get("type", "function"),
                                "function": {"name": "", "arguments": ""},
                            }
                            tool_calls_by_index[tc_idx] = slot
                        if tc_delta.get("id"):
                            slot["id"] = tc_delta["id"]
                        if tc_delta.get("type"):
                            slot["type"] = tc_delta["type"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = (
                                slot["function"].get("name", "") + fn["name"]
                            )
                            # Fire the tool-name callback the first time we
                            # see it. This is the *old* path kept for
                            # backwards-compat (legacy callers still use it).
                            # The new ``on_token`` path below also surfaces
                            # the tool name to the per-token progress callback
                            # so the UI can switch from "model is thinking"
                            # to "<tool_name>: …" the moment the model
                            # commits to a tool.
                            if on_tool_call_name is not None and not slot.get(
                                "_name_sent"
                            ):
                                slot["_name_sent"] = True
                                cb = on_tool_call_name
                                args = (slot["function"]["name"], "")
                                try:
                                    asyncio.create_task(_safe_call(cb, *args))
                                except RuntimeError:
                                    with contextlib.suppress(Exception):
                                        await cb(*args)
                            # Same signal on the new per-token path. The
                            # token callback is fire-and-forget so a slow
                            # Discord edit doesn't stall the SSE read.
                            if on_token is not None and not slot.get(
                                "_token_name_sent"
                            ):
                                slot["_token_name_sent"] = True
                                try:
                                    on_token(
                                        {
                                            "content": "",
                                            "reasoning": "",
                                            "tool_name": slot["function"]["name"],
                                        }
                                    )
                                except Exception:
                                    pass
                        if fn.get("arguments"):
                            slot["function"]["arguments"] = (
                                slot["function"].get("arguments", "") + fn["arguments"]
                            )
                            # Surface the model's reasoning mid-stream so the
                            # progress message shows intent (not a static
                            # "generating…") during long argument generation
                            # (e.g. create_site's HTML body). Reasoning is
                            # usually the first field emitted, so it completes
                            # well before the big fields. Fires once per call.
                            if on_tool_call_name is not None and not slot.get(
                                "_reasoning_sent"
                            ):
                                reason = _extract_partial_reasoning(
                                    slot["function"]["arguments"]
                                )
                                if reason:
                                    slot["_reasoning_sent"] = True
                                    cb = on_tool_call_name
                                    args = (slot["function"]["name"], reason)
                                    try:
                                        asyncio.create_task(
                                            _safe_call(cb, *args)
                                        )
                                    except RuntimeError:
                                        with contextlib.suppress(Exception):
                                            await cb(*args)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            # Some providers stream usage in the final frame (Anthropic-style
            # models on OpenRouter do this; OpenAI does it when
            # stream_options.include_usage=true).
            if obj.get("usage"):
                merged["usage"] = obj["usage"]
        else:
            # No inner break — keep iterating. Outer loop continues.
            continue
        # Inner break hit [DONE]; stop reading.
        break

    if (
        not tool_calls_by_index
        and not content_parts
        and not role
        and finish_reason is None
    ):
        raise RuntimeError("Provider stream produced no choices")

    # Sort tool calls by their index so the order matches the model's intent.
    # Strip the internal callback-tracking flags ("_name_sent"/"_reasoning_sent")
    # so they never leak into the tool_calls we hand back to the provider.
    tool_calls_list = [
        {
            k: v
            for k, v in tool_calls_by_index[idx].items()
            if not str(k).startswith("_")
        }
        for idx in sorted(tool_calls_by_index)
    ]
    message: dict = {"role": role or "assistant"}
    if content_parts:
        message["content"] = "".join(content_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls_list:
        message["tool_calls"] = tool_calls_list

    # The first (and typically only) choice carries the finished message.
    merged["choices"][0] = {
        "index": 0,
        "message": message,
        "finish_reason": finish_reason,
    }
    merged["__first_token_s__"] = first_token_s
    return merged


# When an endpoint returns a 429 (rate-limited / usage-exhausted), we temporarily
# steer traffic away from it for this long instead of retrying it in the same
# request. This avoids hammering a shared upstream pool (e.g. OpenRouter's
# pooled free keys) that is already rate-limiting us, which only makes the
# limit worse. Override via OLLAMA_ENDPOINT_COOLDOWN_SECONDS.
DEFAULT_ENDPOINT_COOLDOWN_SECONDS = 60.0

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


class ProviderResult(str):
    """A ``str`` subclass carrying per-call ``tool_calls`` / ``usage``.

    Behaves exactly like a ``str`` everywhere a string is expected (f-strings,
    ``len()``, ``or ""``, ``str()``, slicing, etc.), but also exposes the
    native tool calls and token usage for *this specific call* so the caller
    does not have to read shared provider instance state.

    Reading ``provider._last_tool_calls`` / ``provider._last_usage`` after an
    ``await`` was racy: with ``ai_concurrency > 1`` (or background ticks sharing
    the same provider), a concurrent ``generate_response`` could overwrite the
    shared state between the call and the consume, causing one channel to
    execute another channel's tool calls. Attaching the values to the returned
    object makes the handoff per-call and race-free.
    """

    __slots__ = ("tool_calls", "usage", "assistant_message")

    def __new__(
        cls,
        content,
        tool_calls: list | None = None,
        usage: dict | None = None,
        assistant_message: dict | None = None,
    ):
        inst = super().__new__(
            cls, content if isinstance(content, str) else str(content or "")
        )
        inst.tool_calls = list(tool_calls) if tool_calls else []
        inst.usage = dict(usage) if usage else {}
        inst.assistant_message = assistant_message
        return inst


def _is_usage_exhausted_error(status: int, error_text: str) -> bool:
    """Detect true quota/credit exhaustion — not ordinary rate limits.

    Transient 429 rate limits must still get normal retry/backoff. Only treat as
    exhausted when the body clearly indicates cooldown, quota, or credits.
    """
    text = (error_text or "").lower()
    # Explicit exhaustion / cooldown markers (avoid bare "usage" / "rate limit").
    markers = (
        "model_cooldown",
        "cooling down",
        "insufficient_quota",
        "insufficient credits",
        "credit balance",
        "quota exceeded",
        "out of credits",
        "out of quota",
        "billing hard limit",
        "spend limit",
    )
    if status != 429:
        return False
    # Ordinary rate limiting is NOT exhausted. Only flag as exhausted when
    # the body also clearly mentions quota/credit markers; rate-limit alone
    # means transient and the caller should keep retrying.
    is_rate_limit = (
        "rate limit" in text or "rate_limit" in text or "too many requests" in text
    )
    is_quota_marker = any(m in text for m in markers)
    if is_rate_limit and not is_quota_marker:
        return False
    return is_quota_marker


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
        enable_audio_input: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = api_key.strip()
        self.retry_attempts = max(1, retry_attempts)
        self.enable_audio_input = bool(enable_audio_input)
        self._endpoints = [
            ProviderEndpoint(
                "primary", self.base_url, self.model, self.api_key, disable_reasoning
            ),
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
        self._last_tool_calls: list = []
        self._last_assistant_message: dict | None = None
        # Per-endpoint learned max *output* token cap (name -> cap). Set when a
        # 400 "maximum output tokens" is observed, and applied proactively on
        # the next call to that endpoint so we don't waste a round-trip on the
        # 400 again. Scoped per-endpoint (NOT on the shared instance) so one
        # model's small output cap doesn't cripple other endpoints/concurrent
        # requests that previously got mutated via self.max_tokens.
        self._endpoint_output_caps: dict[str, int] = {}
        # Per-endpoint rate-limit cooldown: name -> monotonic expiry. While an
        # endpoint is cooling, _attempt_endpoint steers to an alternative (if
        # any) so a rate-limited upstream isn't retried immediately.
        self._endpoint_cooldown: dict[str, float] = {}
        try:
            self._cooldown_seconds = float(
                os.getenv(
                    "OLLAMA_ENDPOINT_COOLDOWN_SECONDS",
                    str(DEFAULT_ENDPOINT_COOLDOWN_SECONDS),
                )
                or DEFAULT_ENDPOINT_COOLDOWN_SECONDS
            )
        except (TypeError, ValueError):
            self._cooldown_seconds = DEFAULT_ENDPOINT_COOLDOWN_SECONDS

    def _headers(self, endpoint: ProviderEndpoint = None) -> dict[str, str]:
        api_key = self.api_key if endpoint is None else endpoint.api_key
        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    def _attempt_endpoint(
        self, attempt: int, *, fast_fallback: bool = False
    ) -> ProviderEndpoint:
        if len(self._endpoints) < 2:
            return self._endpoints[0]
        if fast_fallback:
            natural = self._endpoints[0] if attempt == 1 else self._endpoints[1]
        else:
            # Attempt 1 and 2: primary (main)
            # Attempt 3 and beyond: fallback (second provider)
            natural = self._endpoints[0] if attempt <= 2 else self._endpoints[1]
        # If the chosen endpoint is rate-limit cooling and a healthy alternative
        # exists, skip straight to it. This turns a 429 on a shared upstream into
        # an immediate fallback instead of a doomed same-endpoint retry.
        if self._is_endpoint_cooling(natural.name):
            for ep in self._endpoints:
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

    def _cool_endpoint(self, name: str) -> None:
        self._endpoint_cooldown[name] = time.monotonic() + self._cooldown_seconds
        logger.warning(
            "Provider endpoint %s rate-limited; cooling for %.0fs (using alternative if available)",
            name,
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
        tools: list[dict] = None,
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        disable_reasoning: bool = None,
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
        data = {
            "model": (model or endpoint.model)
            if endpoint.name == "primary"
            else endpoint.model,
            "messages": chat_messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": True,
        }
        # Always include max_tokens from config or override
        effective_max = max_tokens if max_tokens is not None else self.max_tokens
        # Proactively clamp to a previously-learned per-endpoint output cap so
        # we don't waste a round-trip re-hitting the same 400. Per-endpoint so a
        # small-cap model never lowers the cap for other endpoints.
        learned_cap = self._endpoint_output_caps.get(endpoint.name)
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
                        logger.info(
                            f"Provider endpoint initialized: {endpoint.name} ({endpoint.model})"
                        )
                    else:
                        logger.warning(
                            f"Provider endpoint {endpoint.name} /models returned {resp.status}"
                        )
            except Exception as e:
                logger.error(
                    f"Provider endpoint {endpoint.name} initialization failed: {e}"
                )
        self.available = initialized
        return initialized

    async def generate_response(
        self,
        messages: list[dict],
        images: list[str] = None,
        media: list[dict] = None,
        timeout: int = 3600,
        on_tool_call_name=None,
        on_token=None,
        **kwargs,
    ) -> str:
        """Generate response. images is legacy b64 list, media is list of {b64, mime_type}.

        When the model returns native OpenAI-style ``tool_calls``, content may be
        empty. Those calls are stored on ``self._last_tool_calls`` (raw provider
        format) and ``self._last_assistant_message`` for the orchestration loop.
        Callers that pass ``tools=`` must check ``_last_tool_calls`` before treating
        empty content as a failure.

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
                    messages, images=images, media=media, timeout=timeout, **kwargs
                )
            else:
                raise

        tool_calls = message.get("tool_calls") or []
        tool_calls = tool_calls if isinstance(tool_calls, list) else []
        # Capture usage synchronously right after the await returns, before any
        # further await can let a concurrent call overwrite shared state. This
        # value is attached to the returned ProviderResult so the caller never
        # has to read the racy shared ``self._last_usage``.
        usage = dict(self._last_usage) if self._last_usage else {}
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
            raise RuntimeError("Empty response from provider")
        return ProviderResult(
            content,
            tool_calls=tool_calls,
            usage=usage,
            assistant_message=message,
        )

    async def generate_chat_completion(
        self,
        messages: list[dict],
        images: list[str] = None,
        media: list[dict] = None,
        tools: list[dict] = None,
        model: str = None,
        timeout: int = 3600,
        max_tokens: int = None,
        temperature: float = None,
        disable_reasoning: bool = None,
        fast_fallback: bool = False,
        on_tool_call_name=None,
        on_token=None,
    ) -> dict:
        """Generate an OpenAI-compatible assistant message, optionally with tools.

        If ``on_tool_call_name`` is provided, it's called (fire-and-forget) the
        first time a tool_call delta with a function name arrives in the SSE
        stream. This lets callers update a live progress message mid-generation.
        """
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
            if not m.get("b64"):
                continue
            if mime.startswith(("image/", "video/")) or (
                mime.startswith("audio/") and getattr(self, "enable_audio_input", False)
            ):
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
                        parts.append({"type": "video_url", "video_url": {"url": uri}})
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
        has_media = bool(payload_media)
        # Endpoints that rejected this call's media (e.g. "No endpoints found that
        # support input audio"). We steer retries away from them so an audio
        # request never dies on a text-only fallback model.
        audio_broken: set[str] = set()
        max_attempts = (
            min(self.retry_attempts, 2)
            if fast_fallback and len(self._endpoints) > 1
            else self.retry_attempts
        )
        for attempt in range(1, max_attempts + 1):
            endpoint = self._attempt_endpoint(attempt, fast_fallback=fast_fallback)
            if endpoint.name in audio_broken:
                usable = [e for e in self._endpoints if e.name not in audio_broken]
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
                        ):
                            continue
                        raise RuntimeError(
                            f"Provider overloaded after retries: {error_text[:200]}"
                        )
                    if resp.status == 429:
                        error_text = await resp.text()
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                        )
                        self._cool_endpoint(endpoint.name)
                        if _is_usage_exhausted_error(resp.status, error_text):
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
                            ):
                                continue
                            raise last_usage_error
                        if await self._retry_after_attempt(
                            attempt,
                            endpoint,
                            f"Provider {endpoint.name} 429 rate limited",
                            max_attempts=max_attempts,
                            fast_fallback=fast_fallback,
                        ):
                            continue
                        raise RuntimeError(
                            f"Provider rate limited after retries: {error_text[:200]}"
                        )
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(
                            "Provider timing status endpoint=%s status=%s headers_ms=%.1f body_chars=%s",
                            endpoint.name,
                            resp.status,
                            headers_ms,
                            len(error_text),
                        )
                        # Fallback model can't handle audio input (OpenRouter 404:
                        # "No endpoints found that support input audio"). Don't die
                        # here — mark the endpoint broken and retry an audio-capable
                        # one (typically the primary) instead.
                        if (
                            has_media
                            and resp.status == 404
                            and "support input audio" in error_text.lower()
                        ):
                            audio_broken.add(endpoint.name)
                            usable = [
                                e for e in self._endpoints if e.name not in audio_broken
                            ]
                            if not usable:
                                raise RuntimeError(
                                    f"Provider {endpoint.name} audio-unsupported and no alternatives: {error_text[:200]}"
                                )
                            if attempt >= max_attempts:
                                max_attempts = attempt + len(usable)
                            logger.warning(
                                "Provider endpoint %s cannot handle audio media; retrying with %s",
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
                            ):
                                continue
                            raise RuntimeError(
                                f"Provider {endpoint.name} degraded and no fallback available: {error_text[:200]}"
                            )
                        # Auto-clamp max_tokens on context overflow (OpenRouter returns 400)
                        if (
                            resp.status == 400
                            and "maximum context length" in error_text.lower()
                            and max_tokens is None
                        ):
                            import re as _re

                            ctx_match = _re.search(
                                r"maximum context length is (\d+) tokens", error_text
                            )
                            req_match = _re.search(
                                r"you requested about (\d+) tokens", error_text
                            )
                            if ctx_match and req_match:
                                ctx_limit = int(ctx_match.group(1))
                                requested = int(req_match.group(1))
                                estimated_input = requested - int(
                                    data.get("max_tokens", self.max_tokens)
                                )
                                safe_output = max(
                                    4096, ctx_limit - estimated_input - 512
                                )
                                if safe_output < int(
                                    data.get("max_tokens", self.max_tokens)
                                ):
                                    logger.warning(
                                        "Clamping max_tokens from %s to %s due to context limit %s",
                                        data.get("max_tokens"),
                                        safe_output,
                                        ctx_limit,
                                    )
                                    # The loop rebuilds payloads every attempt. Mutating only
                                    # data["max_tokens"] here is a fake fix; keep the clamp in
                                    # loop state or we retry the same busted request like idiots.
                                    max_tokens = safe_output
                                    data["max_tokens"] = safe_output
                                    if await self._retry_after_attempt(
                                        attempt,
                                        endpoint,
                                        f"Context overflow, clamped max_tokens to {safe_output}",
                                        max_attempts=max_attempts,
                                        fast_fallback=fast_fallback,
                                    ):
                                        continue
                        # max_tokens is *output* length, not context. Models like
                        # minimax-m3 can have 1M context but only e.g. 131072 max output.
                        if resp.status == 400 and (
                            "maximum output tokens" in error_text.lower()
                            or "exceeds model's maximum output" in error_text.lower()
                        ):
                            import re as _re

                            out_match = _re.search(
                                r"maximum output tokens\s*\(?\s*(\d+)\s*\)?",
                                error_text,
                                _re.IGNORECASE,
                            )
                            if not out_match:
                                out_match = _re.search(
                                    r"maximum output tokens \((\d+)\)",
                                    error_text,
                                    _re.IGNORECASE,
                                )
                            if out_match:
                                out_cap = int(out_match.group(1))
                                # Leave headroom under the hard cap.
                                safe_output = max(1024, min(out_cap - 64, out_cap))
                                current = int(data.get("max_tokens", self.max_tokens))
                                if safe_output < current:
                                    logger.warning(
                                        "Clamping max_tokens from %s to %s (model max output %s)",
                                        current,
                                        safe_output,
                                        out_cap,
                                    )
                                    max_tokens = safe_output
                                    # Remember per-endpoint so future calls to
                                    # this endpoint clamp proactively without a
                                    # wasted 400 round-trip. Do NOT mutate the
                                    # shared self.max_tokens: that permanently
                                    # crippled every other endpoint/concurrent
                                    # request after one small-cap model was hit.
                                    self._endpoint_output_caps[endpoint.name] = (
                                        safe_output
                                    )
                                    data["max_tokens"] = safe_output
                                    if await self._retry_after_attempt(
                                        attempt,
                                        endpoint,
                                        f"Output cap, clamped max_tokens to {safe_output}",
                                        max_attempts=max_attempts,
                                        fast_fallback=True,
                                    ):
                                        continue
                        raise RuntimeError(
                            f"Provider API error: {resp.status} - {error_text}"
                        )

                    json_ms = 0.0
                    if data.get("stream"):
                        merged = await _read_sse_response(
                            resp,
                            on_tool_call_name=on_tool_call_name,
                            on_token=on_token,
                        )
                        result = {
                            k: v for k, v in merged.items() if not k.startswith("__")
                        }
                        first_token_s = merged.get("__first_token_s__")
                        # Streaming has no JSON-parse step; report the
                        # time-to-first-token so the latency log stays useful
                        # instead of fabricating a json_ms value.
                        if first_token_s is not None:
                            json_ms = (first_token_s - request_start) * 1000
                    else:
                        result = await resp.json()
                        json_ms = (time.perf_counter() - request_start) * 1000
                    if not isinstance(result, dict):
                        result_preview = (
                            str(result)[:600] if result is not None else "None"
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
                        result_preview = str(result)[:600] if result else ""
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
                                str(err_obj)[:300],
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

                    message = choices[0].get("message", {})
                    content = message.get("content", "")
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
                        ):
                            continue
                        raise RuntimeError("Empty response from provider")

                    usage = result.get("usage", {})
                    self._last_usage = {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    }
                    # Healthy response: this endpoint is no longer rate-limited.
                    self._endpoint_cooldown.pop(endpoint.name, None)
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
                logger.warning(
                    "Provider timing timeout endpoint=%s elapsed_ms=%.1f timeout=%s",
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
                ):
                    continue
                raise RuntimeError(
                    f"Provider request timed out after {timeout}s"
                ) from asyncio.TimeoutError
            except ProviderUsageExhaustedError:
                raise
            except RuntimeError as e:
                last_error = e
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} error: {e}",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                ):
                    continue
                raise
            except Exception as e:
                last_error = e
                if await self._retry_after_attempt(
                    attempt,
                    endpoint,
                    f"Provider {endpoint.name} error: {e}",
                    max_attempts=max_attempts,
                    fast_fallback=fast_fallback,
                ):
                    continue
                raise RuntimeError(f"Provider call failed: {last_error}") from e
        if last_usage_error:
            raise last_usage_error
        raise RuntimeError("Provider call failed after retries")

    async def _retry_after_attempt(
        self,
        attempt: int,
        endpoint: ProviderEndpoint,
        reason: str,
        *,
        max_attempts: int = None,
        fast_fallback: bool = False,
    ) -> bool:
        max_attempts = max_attempts or self.retry_attempts
        if attempt >= max_attempts:
            return False
        next_endpoint = self._attempt_endpoint(attempt + 1, fast_fallback=fast_fallback)
        if self._should_wait_before_retry(endpoint, next_endpoint):
            wait = attempt * 2
            logger.warning(
                f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying in {wait}s..."
            )
            await asyncio.sleep(wait)
        else:
            logger.warning(
                f"{reason} (attempt {attempt}/{self.retry_attempts}), retrying with {next_endpoint.name} provider..."
            )
        return True
