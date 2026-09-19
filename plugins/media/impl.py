"""Tool implementations for the media plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

class SeeImageTool(Tool):
    """Download an image/GIF URL and attach it as vision on the next turn."""
    tool_name = 'see_image'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Look at an image or GIF by URL and attach it to your next turn so "
            "you can actually see it. Use for Tenor/Giphy/imgur GIF pages, "
            "Discord CDN links, or any direct jpg/png/gif/webp that was not "
            "already attached to the message. Prefer this over fetch_url for "
            "pictures. Params: url (required)."
        )

    @classmethod
    def looks_visual(cls, url: str) -> bool:
        return is_gif_page_url(url) or is_direct_image_url(url)

    async def result_from_blob(
        self,
        blob: bytes,
        mime: str,
        url: str,
        message: Message | None = None,
        filename: str = "",
    ) -> str:
        if not blob:
            return "Error: empty image"
        control = getattr(self.bot, "_control", None) or {}
        if not parse_bool(control.get("process_images"), True):
            return "Error: image processing is disabled"
        ext = Path(urlparse(url).path).suffix.lower() or {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
        }.get(mime, ".bin")
        filename = filename or f"see-image{ext}"
        max_size = 10 * 1024 * 1024
        if self.bot is not None and hasattr(self.bot, "_max_media_bytes"):
            with contextlib.suppress(Exception):
                max_size = self.bot._max_media_bytes()
        if (
            self.bot is not None
            and hasattr(self.bot, "_normalize_gif")
            and (
                mime in {"image/gif", "video/mp4", "video/webm"}
                or ext in {".gif", ".mp4", ".webm"}
            )
        ):
            normalized = await self.bot._normalize_gif(blob, filename, max_size)
            if normalized:
                blob, mime, filename = normalized
        if not mime.startswith("image/"):
            return (
                f"Error: URL was {mime or 'unknown type'}, not an image I can look at"
            )
        encoded = base64.b64encode(blob).decode("ascii")
        if (
            self.bot is not None
            and message is not None
            and hasattr(self.bot, "_cache_media_context")
            and hasattr(self.bot, "_media_item")
        ):
            with contextlib.suppress(Exception):
                channel_id = str(
                    getattr(getattr(message, "channel", None), "id", "") or ""
                )
                item = self.bot._media_item(
                    b64=encoded,
                    mime_type=mime,
                    filename=filename,
                    is_image=True,
                    message_id=getattr(message, "id", None),
                    source="see_image",
                    url=url,
                )
                if channel_id:
                    self.bot._cache_media_context(channel_id, [item])
        return (
            f"Attached {filename} ({mime}) for visual inspection.\n"
            f"Source: {url}\n"
            f"__IMAGE_B64__{encoded}__END_IMAGE_B64__"
        )

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if not url:
            return "Error: url is required"
        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"
        control = getattr(self.bot, "_control", None) or {}
        if not parse_bool(control.get("process_images"), True):
            return "Error: image processing is disabled"
        if self.bot is None or not hasattr(self.bot, "_download_embed_media"):
            return "Error: see_image is unavailable"
        max_size = 10 * 1024 * 1024
        if hasattr(self.bot, "_max_media_bytes"):
            with contextlib.suppress(Exception):
                max_size = self.bot._max_media_bytes()
        item = await self.bot._download_embed_media(
            url, "see-image", max_size, getattr(message, "id", None)
        )
        if not item or not item.get("b64"):
            return f"Error: could not load an image from {url}"
        mime = str(item.get("mime_type") or "")
        if mime.startswith("video/"):
            blob = base64.b64decode(item["b64"], validate=True)
            return await SeeVideoTool(self.bot).result_from_blob(
                blob,
                mime,
                url,
                message,
                filename=str(item.get("filename") or ""),
            )
        if not item.get("is_image") or not mime.startswith("image/"):
            return f"Error: URL was {mime or 'unknown type'}, not an image I can look at"
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if channel_id and hasattr(self.bot, "_cache_media_context"):
            with contextlib.suppress(Exception):
                self.bot._cache_media_context(channel_id, [item])
        return (
            f"Attached {item.get('filename', 'image')} ({item.get('mime_type')}) "
            f"for visual inspection.\n"
            f"Source: {item.get('url') or url}\n"
            f"__IMAGE_B64__{item['b64']}__END_IMAGE_B64__"
        )

class SeeVideoTool(Tool):
    """Download a video URL and attach ffmpeg-derived frames for vision."""
    tool_name = 'see_video'
    returns_result = True
    ends_turn = False


    VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi"})
    AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"})

    def get_description(self):
        return (
            "Look at a direct video URL by extracting representative frames "
            "with ffmpeg, and include its audio when audio input is enabled. "
            "Use for mp4/webm/mov links or video embeds. YouTube links are not "
            "downloaded. Params: url (required)."
        )

    @classmethod
    def looks_video(cls, url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
            if parsed.scheme not in {"http", "https"}:
                return False
            # Keep the dedicated YouTube tool authoritative even if a pasted
            # URL has an unusual path suffix.
            host = (parsed.hostname or "").lower()
            if host in {
                "youtu.be",
                "youtube.com",
                "youtube-nocookie.com",
            } or host.endswith((".youtube.com", ".youtube-nocookie.com")):
                return False
            return Path(parsed.path).suffix.lower() in cls.VIDEO_EXTS
        except Exception:
            return False

    def _audio_enabled(self) -> bool:
        control = getattr(self.bot, "_control", None) or {}
        if isinstance(control, dict) and "process_audio" in control:
            return parse_bool(control.get("process_audio"), False)
        return parse_bool(
            getattr(getattr(self.bot, "config", None), "ENABLE_AUDIO_INPUT", False),
            False,
        )

    def _max_size(self) -> int:
        max_size = 10 * 1024 * 1024
        if self.bot is not None and hasattr(self.bot, "_max_media_bytes"):
            with contextlib.suppress(Exception):
                max_size = self.bot._max_media_bytes()
        return max_size

    async def result_from_blob(
        self,
        blob: bytes,
        mime: str,
        url: str,
        message: Message | None = None,
        filename: str = "",
    ) -> str:
        if not blob:
            return "Error: empty video"
        if self.bot is None or not hasattr(self.bot, "_extract_video_derivatives"):
            return "Error: see_video is unavailable"
        control = getattr(self.bot, "_control", None) or {}
        if not parse_bool(
            getattr(getattr(self.bot, "config", None), "ENABLE_VIDEO_INPUT", True),
            True,
        ):
            return "Error: video input is disabled"
        process_images = parse_bool(control.get("process_images"), True)
        if not process_images and not self._audio_enabled():
            return "Error: image and audio processing are disabled"
        max_size = self._max_size()
        if len(blob) > max_size:
            return "Error: video exceeds the configured media size limit"
        name = filename or f"see-video{Path(urlparse(url).path).suffix or '.mp4'}"
        derived = await self.bot._extract_video_derivatives(
            blob,
            name,
            getattr(message, "id", None),
            max_size,
            source_url=url,
            include_frames=process_images,
            source_prefix="see_video",
        )
        if not derived:
            return f"Error: could not extract frames/audio from {url}"
        if self.bot is not None and message is not None:
            channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
            if channel_id and hasattr(self.bot, "_cache_media_context"):
                with contextlib.suppress(Exception):
                    self.bot._cache_media_context(
                        channel_id,
                        [item for item in derived if item.get("is_image")],
                    )
        lines = [
            f"Extracted {sum(1 for item in derived if item.get('is_image'))} "
            f"video frame(s) from {url}."
        ]
        if any(
            str(item.get("mime_type") or "").startswith("audio/") for item in derived
        ):
            lines.append("An audio track was extracted for audio-capable input.")
        for item in derived:
            if item.get("is_image") and item.get("b64"):
                lines.append(f"__IMAGE_B64__{item['b64']}__END_IMAGE_B64__")
            elif str(item.get("mime_type") or "").startswith("audio/") and item.get(
                "b64"
            ):
                # Tool follow-up parsing turns this into an input_audio media
                # part; keep the marker out of the user-facing transcript.
                lines.append(f"__AUDIO_B64__{item['b64']}__END_AUDIO_B64__")
        return "\n".join(lines)

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if not url:
            return "Error: url is required"
        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"
        if _is_youtube_url(url):
            return "Error: YouTube URLs are not downloaded"
        control = getattr(self.bot, "_control", None) or {}
        if (
            not parse_bool(control.get("process_images"), True)
            and not self._audio_enabled()
        ):
            return "Error: image and audio processing are disabled"
        if self.bot is None or not hasattr(self.bot, "_download_embed_media"):
            return "Error: see_video is unavailable"
        max_size = self._max_size()
        item = await self.bot._download_embed_media(
            url,
            "see-video" + (Path(urlparse(url).path).suffix or ".mp4"),
            max_size,
            getattr(message, "id", None),
        )
        if not item or not item.get("b64"):
            return f"Error: could not load a video from {url}"
        mime = str(item.get("mime_type") or "")
        if not mime.startswith("video/"):
            return f"Error: URL was {mime or 'unknown type'}, not a video"
        try:
            blob = base64.b64decode(item["b64"], validate=True)
        except (ValueError, TypeError):
            return "Error: downloaded video was invalid"
        return await self.result_from_blob(
            blob,
            mime,
            url,
            message,
            filename=str(item.get("filename") or ""),
        )

class SendMemeTool(Tool):
    """Send a random meme from Reddit"""
    tool_name = 'send_meme'
    returns_result = True
    ends_turn = False
    produces_visible_output = True


    MEME_API = "https://meme-api.com/gimme"
    MAX_SIZE = 25 * 1024 * 1024

    def get_description(self):
        return (
            "Send a random meme from Reddit. Params: subreddit (optional, e.g. 'me_irl', 'dankmemes'). "
            "No params = random from r/memes."
        )

    async def execute(
        self, message: Message, subreddit: str | None = None, **kwargs
    ) -> str:
        url = self.MEME_API
        if subreddit:
            sub = subreddit.strip().removeprefix("r/")
            if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", sub):
                return "Error: invalid subreddit name"
            url = f"{self.MEME_API}/{sub}"

        try:
            session = await _get_shared_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return f"Error: meme API returned {resp.status}"
                data = await resp.json()
        except Exception as e:
            return f"Error fetching meme: {e}"

        meme_url = data.get("url")
        title = data.get("title", "meme")
        sub = data.get("subreddit", "memes")
        ups = data.get("ups", 0)
        nsfw = data.get("nsfw", False)

        if nsfw:
            return "Error: got an NSFW meme, skipping"

        if not meme_url:
            return "Error: no meme URL in response"

        if not _is_safe_url(meme_url):
            return "Error: meme API returned an unsafe media URL"

        try:
            async with session.get(
                meme_url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
            ) as img_resp:
                if img_resp.status != 200:
                    return f"Error: could not download meme image ({img_resp.status})"
                img_bytes = await _read_response_limited(img_resp, self.MAX_SIZE)
        except Exception as e:
            return f"Error downloading meme: {e}"

        filename = meme_url.rsplit("/", 1)[-1].split("?")[0] or "meme.png"
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"):
            filename += ".png"

        file = File(BytesIO(img_bytes), filename=filename)
        try:
            sent = await message.reply(file=file)
            record_delivery = getattr(self.bot, "_record_delivery", None)
            if callable(record_delivery) and sent is not None:
                record_delivery(message, sent)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending meme: {e}"

        return f'__MEME_SENT__ Sent meme: "{title}" from r/{sub} ({ups} upvotes)'

class SendMediaTool(Tool):
    """Send an image/video from a URL as a Discord attachment"""
    tool_name = 'send_media'
    returns_result = True
    ends_turn = False
    produces_visible_output = True


    MAX_SIZE = 25 * 1024 * 1024

    def get_description(self):
        return (
            "Send an image/video URL as a Discord attachment. "
            "Params: url (required, direct link to media file)."
        )

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if not url:
            return "Error: url is required"

        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"

        try:
            session = await _get_shared_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return f"Error: HTTP {resp.status}"
                media_bytes = await _read_response_limited(resp, self.MAX_SIZE)
        except asyncio.TimeoutError:
            return f"Error: timed out downloading {url}"
        except Exception as e:
            return f"Error downloading: {e}"

        filename = _safe_attachment_filename(
            url.rsplit("/", 1)[-1].split("?")[0], default="media"
        )
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".mp4",
            ".webm",
            ".weba",
            ".mp3",
        ):
            # Unknown extension: don't disguise it as a PNG; use a generic safe suffix.
            # Discord still transports the raw bytes, so this is only a naming hint.
            logger.warning(
                f"SendMediaTool normalizing unknown extension {ext!r} to .bin"
            )
            filename = os.path.splitext(filename)[0] + ".bin"

        file = File(BytesIO(media_bytes), filename=filename)
        sent = None
        try:
            sent = await message.reply(file=file)
            record_delivery = getattr(self.bot, "_record_delivery", None)
            if callable(record_delivery) and sent is not None:
                record_delivery(message, sent)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending media: {e}"

        # Attach the URL of what was actually sent (source URL + the new
        # Discord CDN URL) so the model can curl/pull/reuse either one.
        cdn_url = ""
        if sent is not None and getattr(sent, "attachments", None):
            cdn_url = sent.attachments[0].url
        result = f"__MEDIA_SENT__ Sent media: {filename}"
        result += f"\nSource URL: {url}"
        if cdn_url:
            result += f"\nFile URL: {cdn_url}"
        return result
