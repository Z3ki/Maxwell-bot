"""Tool implementations for the images plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool
from generated_artifacts import register_generated_file

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name


def _generated_image_result(bot, image_bytes: bytes, *, prefix: str, summary: str) -> str:
    """Return model-visible media and reusable links without a Discord send."""
    mime = _sniff_image_mime(image_bytes)
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")
    local_path, perm_url = _persist_public_image(bot, image_bytes, ext=ext, prefix=prefix)
    attachment_path = register_generated_file(image_bytes, f"{prefix}{ext}")
    preview = image_bytes
    # The tool-media dispatcher accepts base64 payloads smaller than 5 MB.
    # Keep the full generated file; resize only an oversized model preview.
    if len(preview) >= 3_750_000:
        try:
            from PIL import Image

            with Image.open(BytesIO(preview)) as image:
                image = image.convert("RGB")
                image.thumbnail((1024, 1024))
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=88)
                preview = buffer.getvalue()
                mime = "image/jpeg"
        except Exception as exc:
            return f"Error: could not prepare generated image for model inspection ({exc})"
    result = summary + "\nNot sent to chat. Inspect the attached image and decide what to do next."
    if perm_url:
        result += f"\nImage URL: {perm_url}\nUse send_media with this URL only if you decide to send the image."
    if attachment_path:
        result += (
            f"\nAttachment path: {attachment_path}\n"
            f'Use send_file(path="{attachment_path}") if you decide to send the image. '
            "This attachment is available only during this request."
        )
    if local_path:
        result += f"\nLocal path: {local_path}"
        if getattr(getattr(bot, "config", None), "ENABLE_CREATE_SITE", False):
            result += f' (pass to create_site as images=[{{"path": "{local_path}"}}] to bundle it into a site)'
    payload = base64.b64encode(preview).decode("ascii")
    return result + f"\n__IMAGE_B64__data:{mime};base64,{payload}__END_IMAGE_B64__"


class ImageGeneratorTool(Tool):
    """Fast image generation using Pollinations (SDXL-Lightning)."""
    tool_name = 'image_generator'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Generate an AI image (~2-5s) — the DEFAULT image tool, text-to-image only. "
            "It CANNOT take an input image: to edit/modify/restyle an existing image, use hd_image. "
            "Params: prompt (required). Returns the image for inspection and a request-local attachment path. "
            "Does not send it to chat. Use send_file with that path to deliver it, or send_media with its URL when hosting is configured."
        )

    async def execute(
        self, message: Message, prompt: str | None = None, **kwargs
    ) -> str:
        if not prompt:
            return "Error: prompt parameter is required"
        # Pollinations is the primary generator — keyless, fast, always up.
        # The long-dead NVIDIA Flux route was dropped (it hung ~6 min per
        # request before timing out).
        return await self._pollinations_generate(message, prompt)


    async def _pollinations_generate(self, message: Message, prompt: str) -> str:
        # Model comes solely from config — which reads POLLINATIONS_MODEL from
        # .env (config default applies only when unset). No hardcoded fallback
        # here so we never silently shift models across code edits.
        model = str(getattr(self.bot.config, "POLLINATIONS_MODEL", "") or "").strip()
        seed = random.randint(0, 999999)
        url = (
            "https://image.pollinations.ai/prompt/"
            f"{quote(prompt[:1500], safe='')}"
            f"?width=1024&height=1024&nologo=true&model={quote(model, safe='')}"
            f"&seed={seed}"
        )
        session = await _get_shared_session()
        try:
            async with session.get(
                url,
                headers={"User-Agent": _IMAGE_FETCH_UA, "Accept": "image/*"},
                timeout=aiohttp.ClientTimeout(total=90),
                allow_redirects=True,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error(
                        "Pollinations image error: %s - %s",
                        response.status,
                        body[:300],
                    )
                    return f"Error generating image: Pollinations returned {response.status}."
                ctype = (
                    (response.headers.get("Content-Type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                raw = await _read_response_limited(response, 12 * 1024 * 1024)
        except asyncio.TimeoutError:
            return "Error: Pollinations image generation timed out."
        except Exception as e:
            logger.warning("Pollinations image error: %s", e)
            return f"Error generating image: {e}"
        looks_like_image = bool(
            raw
            and (
                raw.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF"))
                or ctype.startswith("image/")
            )
        )
        if not looks_like_image:
            logger.error(
                "Pollinations returned non-image payload (%s, %s bytes)",
                ctype,
                len(raw or b""),
            )
            return "Error: Pollinations did not return an image."
        logger.info(
            "Pollinations image generated successfully, size: %s bytes", len(raw)
        )
        return _generated_image_result(
            self.bot, raw, prefix="pollinations", summary=f"Image generated: {prompt[:100]}"
        )

class HDImageGeneratorTool(Tool):
    """HD image generation and editing via the Gemini image model.

    Talks to the OpenAI-compatible chat endpoint rather than
    /images/generations: only the chat route accepts an input image, so
    generate and edit are the same call with or without an `image` part.
    """
    tool_name = 'hd_image'
    returns_result = True
    ends_turn = False


    # Discord's own limit is 25MB; inputs get downscaled well below it.
    MAX_INPUT_BYTES = 20 * 1024 * 1024
    # An empty response is usually a silent safety refusal, which repeats
    # deterministically (6/6 in testing on a photo of a real person). Retry
    # once for a genuinely flaky gateway, then stop burning ~20s a try.
    MAX_ATTEMPTS = 2
    _DATA_URI_RE = re.compile(
        r"data:image/(?P<ext>[A-Za-z0-9.+-]+);base64,(?P<b64>[A-Za-z0-9+/=]+)"
    )
    _IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def get_description(self):
        return (
            "Generate OR edit an HD AI image with Gemini (~10-30s). Use for high quality/HD/HQ "
            "requests, and for ANY edit of an existing image ('make the car red', 'add a hat', "
            "'remove the background', 'combine these'). "
            "Params: prompt (required — for an edit, describe the change, not the whole scene); "
            "image (optional — an http(s) URL, a local path, or a list of up to 4 of them, to edit "
            "or use as reference). If image is omitted and the user attached images to the message, "
            "those are used automatically. Returns the image for inspection and a request-local attachment path. "
            "Does not send it to chat. Use send_file with that path to deliver it, or send_media with its URL when hosting is configured."
        )

    def _endpoint(self) -> tuple[str, str, str]:
        """(chat_completions_url, api_key, model), inheriting the primary endpoint."""
        cfg = self.bot.config
        base = (
            getattr(cfg, "GEMINI_IMAGE_BASE_URL", "")
            or getattr(cfg, "AI_BASE_URL", "")
            or ""
        ).rstrip("/")
        key = getattr(cfg, "GEMINI_IMAGE_API_KEY", "") or getattr(
            cfg, "AI_API_KEY", ""
        )
        model = getattr(cfg, "GEMINI_IMAGE_MODEL", "") or "gemini-3.1-flash-image"
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        return url, key, model

    def _shrink(self, raw: bytes) -> tuple[bytes, str]:
        """Downscale an input image so the upload does not dominate latency.

        Best effort: without Pillow the original bytes go up untouched, which
        still works, just slower.
        """
        max_edge = int(getattr(self.bot.config, "GEMINI_IMAGE_MAX_INPUT_EDGE", 1024))
        try:
            from PIL import Image as _PILImage

            im = _PILImage.open(BytesIO(raw))
            im = im.convert("RGB")
            if max(im.size) > max_edge:
                ratio = max_edge / float(max(im.size))
                im = im.resize(
                    (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                    _PILImage.LANCZOS,
                )
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=88)
            return buf.getvalue(), "image/jpeg"
        except Exception as e:
            # No Pillow (or an image it cannot open): send the bytes through
            # untouched, but label them from their magic number rather than
            # guessing — a JPEG announced as image/png gets rejected upstream.
            logger.debug(f"hd_image input downscale skipped: {e}")
            return raw, _sniff_image_mime(raw)

    async def _load_one(self, ref: str) -> tuple[bytes | None, str]:
        """Resolve a single image reference to bytes. Returns (bytes, error)."""
        ref = str(ref).strip()
        if not ref:
            return None, "empty image reference"

        # Already-inlined data URI — accept it as-is.
        m = self._DATA_URI_RE.search(ref)
        if m:
            try:
                return base64.b64decode(m.group("b64")), ""
            except Exception:
                return None, "malformed data: URI"

        if ref.startswith(("http://", "https://")):
            if not _is_safe_url(ref):
                return None, f"refusing to fetch private/internal URL {ref[:80]}"
            try:
                session = await _get_shared_session()
                # A redirect is not re-checked by _is_safe_url, so a public URL
                # could otherwise bounce us to link-local metadata. Refuse to
                # follow, same as SendMediaTool. Many image hosts (Wikimedia,
                # Reddit) also 403 a default aiohttp UA, hence the browser one.
                async with session.get(
                    ref,
                    timeout=aiohttp.ClientTimeout(total=60),
                    allow_redirects=False,
                    headers={
                        "User-Agent": _IMAGE_FETCH_UA,
                        "Accept": "image/*,*/*;q=0.8",
                    },
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        return None, (
                            f"{ref[:80]} redirects; pass the direct image URL"
                        )
                    if resp.status != 200:
                        return None, f"HTTP {resp.status} fetching {ref[:80]}"
                    return (
                        await _read_response_limited(resp, self.MAX_INPUT_BYTES)
                    ), ""
            except asyncio.TimeoutError:
                return None, f"timed out fetching {ref[:80]}"
            except Exception as e:
                return None, f"could not fetch {ref[:80]}: {e}"

        # Local path — only from the dirs Maxwell itself writes images to.
        try:
            img_dir, _ = _public_image_target(self.bot)
            allowed = [img_dir, "temp"]
            if not any(_is_path_allowed(ref, root) for root in allowed):
                return None, f"local path outside the allowed image dirs: {ref[:80]}"
            path = Path(ref).resolve()
            if path.stat().st_size > self.MAX_INPUT_BYTES:
                return None, f"file too large: {ref[:80]}"
            # Off-thread: images run up to MAX_INPUT_BYTES and this is on the
            # event loop, so a blocking read stalls unrelated chats.
            return await asyncio.to_thread(path.read_bytes), ""
        except Exception as e:
            return None, f"could not read {ref[:80]}: {e}"

    def _attached_images(self, message: Message) -> list[str]:
        """Image URLs attached to the triggering message."""
        urls = []
        for att in getattr(message, "attachments", None) or []:
            ctype = (getattr(att, "content_type", "") or "").lower()
            name = (getattr(att, "filename", "") or "").lower()
            if ctype.startswith("image/") or name.endswith(self._IMAGE_EXTS):
                url = getattr(att, "url", None)
                if url:
                    urls.append(url)
        return urls

    async def execute(
        self,
        message: Message,
        prompt: str | None = None,
        image: str | list | None = None,
        **kwargs,
    ) -> str:
        if not prompt:
            return "Error: prompt parameter is required"

        api_url, api_key, model = self._endpoint()
        if not api_url or api_url == "/chat/completions":
            return "Error: HD image generation is not configured (no GEMINI_IMAGE_BASE_URL or AI_BASE_URL)"

        # Normalize the image param: a single ref, a list, or a
        # comma/newline-separated string all mean the same thing.
        refs: list[str] = []
        if isinstance(image, (list, tuple)):
            refs = [str(x) for x in image if str(x).strip()]
        elif isinstance(image, str) and image.strip():
            text = image.strip()
            if self._DATA_URI_RE.search(text):
                refs = [text]
            elif text.startswith("["):
                # The schema advertises a JSON list for multiple images.
                try:
                    parsed = json.loads(text)
                    refs = [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    refs = [text]
            else:
                # Split on newlines, and on a comma only where the next ref
                # plainly begins — a bare comma split would corrupt any single
                # URL that carries one in its query string.
                refs = [
                    part.strip()
                    for line in re.split(r"\n+", text)
                    for part in re.split(r",\s*(?=https?://|/)", line)
                    if part.strip()
                ]
        if not refs:
            refs = self._attached_images(message)
        refs = refs[:4]  # keep the payload (and the latency) sane

        parts: list[dict] = [{"type": "text", "text": prompt}]
        loaded = 0
        for ref in refs:
            raw, err = await self._load_one(ref)
            if raw is None:
                logger.warning(f"hd_image input rejected: {err}")
                return f"Error: {err}"
            shrunk, mime = self._shrink(raw)
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{base64.b64encode(shrunk).decode()}"
                    },
                }
            )
            loaded += 1

        payload = {"model": model, "messages": [{"role": "user", "content": parts}]}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout_s = int(getattr(self.bot.config, "GEMINI_IMAGE_TIMEOUT", 300))
        session = await _get_shared_session()

        # This model can spend its whole turn reasoning and return
        # finish_reason=stop with empty content — no refusal text, no image.
        # Editing a photo of a real person reproduces it every time; that is
        # a safety refusal the gateway does not label. Retry once anyway, in
        # case the gateway itself hiccuped.
        found: list[tuple[str, str]] = []
        said = ""
        last_error = ""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                async with session.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        logger.error(
                            f"HD image API error: {response.status} - {body[:500]}"
                        )
                        if "quota" in body.lower():
                            return (
                                f"Error: the HD image model ({model}) has no quota "
                                "right now. Use image_generator instead."
                            )
                        last_error = (
                            "Error generating HD image: API returned status "
                            f"{response.status}"
                        )
                        if 500 <= response.status < 600:
                            continue
                        return last_error
                    try:
                        data = json.loads(body)
                    except Exception:
                        logger.error(f"HD image non-JSON response: {body[:300]}")
                        return "Error: HD image endpoint returned a non-JSON response"
            except asyncio.TimeoutError:
                logger.warning(
                    f"HD image timed out after {timeout_s}s "
                    f"(attempt {attempt + 1}/{self.MAX_ATTEMPTS})"
                )
                last_error = f"Error: HD image generation timed out after {timeout_s}s"
                continue
            except Exception as e:
                logger.error(f"HD image generation request error: {e}")
                return f"Error generating HD image: {e}"

            choices = data.get("choices") or []
            if not choices:
                logger.error(f"HD image response has no choices: {list(data.keys())}")
                last_error = "Error: No image data in HD response"
                continue
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                # Some gateways hand back structured parts, not a string.
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            content = content or ""

            found = self._DATA_URI_RE.findall(content)
            if found:
                break

            said = re.sub(r"\s+", " ", str(content)).strip()
            logger.warning(
                f"HD image attempt {attempt + 1}/{self.MAX_ATTEMPTS} returned no "
                f"image. Text: {said[:200]!r}"
            )
            if said:
                # Actual words back means a refusal or a misread instruction,
                # not the empty-response glitch — retrying just repeats it.
                return (
                    "Error: the HD image model returned text, not an image: "
                    f"{said[:300]}"
                )

        if not found:
            if last_error:
                return last_error
            if loaded:
                return (
                    "Error: the HD image model returned no image. It silently "
                    "refuses to edit photos of real people — say so if that is "
                    "what was asked. Otherwise reword the edit, or use "
                    "image_generator to make a fresh image."
                )
            return (
                "Error: the HD image model returned no image. Reword the prompt, "
                "or use image_generator instead."
            )

        ext, b64 = found[0]
        try:
            image_bytes = base64.b64decode(b64)
        except Exception as e:
            logger.error(f"HD image base64 decode failed: {e}")
            return "Error: HD image data was not decodable"

        verb = "Edited" if loaded else "Generated"
        summary = f"HD image {verb.lower()} successfully: {prompt[:100]}"
        if loaded:
            summary += f" (from {loaded} input image{'s' if loaded > 1 else ''})"
        return _generated_image_result(self.bot, image_bytes, prefix="hd", summary=summary)
