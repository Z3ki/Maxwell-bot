"""Ollama AI Provider for Maxwell Bot"""

import asyncio
import aiohttp
import logging

logger = logging.getLogger(__name__)

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


class OllamaProvider:
    """OpenAI-compatible LLM Provider with multimodal support using /v1/chat/completions"""

    def __init__(self, base_url: str, model: str, max_tokens: int, temperature: float, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = api_key.strip()
        self._session = None
        self.available = False

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def initialize(self):
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/models",
                timeout=aiohttp.ClientTimeout(total=10),
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    self.available = True
                    logger.info(f"Provider initialized: {self.model}")
                    return True
                else:
                    logger.warning(f"Provider /models returned {resp.status}")
        except Exception as e:
            self.available = False
            logger.error(f"Provider initialization failed: {e}")
        return False

    async def generate_response(
        self, messages: list[dict], images: list[str] = None, media: list[dict] = None, timeout: int = 60
    ) -> str:
        """Generate response. images is legacy b64 list, media is list of {b64, mime_type}."""
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
                        audio_format = AUDIO_FORMATS.get(mime.split(";", 1)[0].lower())
                        if audio_format:
                            parts.append({"type": "input_audio", "input_audio": {"data": b64, "format": audio_format}})
                        else:
                            parts.append({"type": "audio_url", "audio_url": {"url": uri}})
                    elif mime.startswith("video/"):
                        parts.append({"type": "file", "file": {"filename": m.get("filename", "video.mp4"), "file_data": b64}})
                    else:
                        continue
                    attached += 1
                target["content"] = parts
                logger.info(f"Attached {attached} multimodal item(s) to message")
            else:
                logger.warning(f"No user message found to attach {len(payload_media)} multimodal item(s)")

        data = {
            "model": self.model,
            "messages": chat_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }

        session = await self._get_session()
        last_error = None
        for attempt in range(1, 3 + 1):
            try:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=timeout, connect=10),
                    headers=self._headers(),
                ) as resp:
                    if resp.status == 503:
                        error_text = await resp.text()
                        if attempt < 3:
                            wait = attempt * 2
                            logger.warning(f"Provider 503 (attempt {attempt}/3), retrying in {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                        raise RuntimeError(f"Provider overloaded after retries: {error_text[:200]}")
                    if resp.status == 429:
                        error_text = await resp.text()
                        if attempt < 3:
                            wait = attempt * 2
                            logger.warning(f"Provider 429 rate limited (attempt {attempt}/3), retrying in {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                        raise RuntimeError(f"Provider rate limited after retries: {error_text[:200]}")
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(
                            f"Provider API error: {resp.status} - {error_text}"
                        )

                    result = await resp.json()
                    choices = result.get("choices", [])
                    if not choices:
                        raise RuntimeError("No response from provider")

                    content = choices[0].get("message", {}).get("content", "")
                    if not content:
                        if attempt < 3:
                            wait = attempt * 2
                            logger.warning(f"Provider returned empty response (attempt {attempt}/3), retrying in {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                        raise RuntimeError("Empty response from provider")

                    return content
            except asyncio.TimeoutError:
                if attempt < 3:
                    wait = attempt * 2
                    logger.warning(f"Provider timeout (attempt {attempt}/3), retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"Provider request timed out after {timeout}s")
            except RuntimeError:
                raise
            except Exception as e:
                last_error = e
                if attempt < 3:
                    wait = attempt * 2
                    logger.warning(f"Provider error (attempt {attempt}/3): {e}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"Provider call failed: {last_error}")
        raise RuntimeError("Provider call failed after retries")
