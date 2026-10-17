"""Inbound media extraction: attachments, embeds, stickers, emoji grids and linked media.

Split out of bot.py; methods keep their original bodies.
"""

import bot_common as _common

# Mechanical split: the methods below were written against bot.py's
# module globals. Bind every shared name so the bodies run unchanged.
for _name in dir(_common):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_common, _name))
del _name

from bot_parts._compat import (  # noqa: F401
    _get_shared_session,
    _read_response_limited,
)

class MediaMixin:

    def _message_carries_media(self, message) -> bool:
        """True when this message or a forwarded snapshot has ingestible media."""
        for source in iter_message_payloads(message):
            if getattr(source, "attachments", None):
                return True
            if getattr(source, "embeds", None):
                return True
            if getattr(source, "stickers", None):
                return True
            if self._media_link_refs(getattr(source, "content", "")):
                return True
        return False

    @staticmethod
    def _payload_attr_list(message, name, cap: int) -> list:
        items = []
        for source in iter_message_payloads(message):
            for item in list(getattr(source, name, None) or []):
                items.append(item)
                if len(items) >= cap:
                    return items
        return items

    async def _read_attachment_bytes(self, attachment, *, max_bytes: int) -> bytes:
        """Read a Discord attachment, including forwarded snapshot stubs.

        Snapshot attachments are often a SimpleNamespace with url/proxy_url
        and no async read(). Fetch those over HTTP instead of crashing.
        """
        read = getattr(attachment, "read", None)
        if callable(read):
            try:
                blob = read()
                if inspect.isawaitable(blob):
                    blob = await blob
                if len(blob) > max_bytes:
                    raise ValueError("attachment exceeds the media size limit")
                return blob
            except discord.HTTPException as exc:
                if exc.status not in {403, 404}:
                    raise
                # An accepted request or cached reply parent can outlive its
                # CDN signature. Reuse the checked, bounded media fetch path.
        urls = list(dict.fromkeys(clean_media_url(getattr(attachment, name, "") or "")
                                  for name in ("url", "proxy_url")))
        urls = [url for url in urls if url]
        if not urls:
            raise AttributeError(f"{type(attachment).__name__} has no read() or url")
        for url in urls:
            fetched = await MaxwellBot._fetch_public_payload(self, url, max_bytes)
            if fetched is not None:
                return fetched[2]
        raise RuntimeError("could not fetch or refresh the attachment URL")

    async def _extract_media(self, message) -> tuple[list[str], list[dict]]:
        proc_img = MaxwellBot._image_input_enabled(self)
        proc_aud = _owner_audio_input_enabled(self)
        # If neither images nor audio processing, skip all binary media collection.
        # (process_audio / ENABLE_AUDIO_INPUT controls audio input to the model)
        if not proc_img and not proc_aud:
            return [], []
        images = []
        media = []
        max_size = self._max_media_bytes()
        image_exts = {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
            ".avif",
            ".apng",
        }
        media_exts = set(MIME_MAP.keys())
        wrapper_att_ids = {
            id(item) for item in list(getattr(message, "attachments", None) or [])
        }
        for attachment in self._payload_attr_list(message, "attachments", 10):
            content_type = getattr(attachment, "content_type", None) or ""
            ext = (
                "." + attachment.filename.rsplit(".", 1)[-1].lower()
                if "." in attachment.filename
                else ""
            )
            is_media = ext in media_exts or content_type.startswith(
                ("image/", "video/", "audio/")
            )
            is_known_text = _is_text_attachment(attachment.filename, content_type)
            # Enforce absolute size limit for ALL attachments including text
            absolute_max = 50 * 1024 * 1024  # 50MB hard cap
            att_size = int(getattr(attachment, "size", 0) or 0)
            if att_size > absolute_max:
                logger.warning(
                    f"Skipping attachment {attachment.filename}: exceeds absolute limit ({att_size} bytes)"
                )
                continue
            if att_size > max_size and not is_known_text:
                logger.warning(
                    f"Skipping attachment {attachment.filename}: too large ({att_size} bytes)"
                )
                continue
            if is_known_text and att_size > TEXT_ATTACHMENT_MAX_BYTES:
                logger.warning(
                    f"Skipping text attachment {attachment.filename}: too large ({att_size} bytes)"
                )
                continue
            try:
                blob = await self._read_attachment_bytes(
                    attachment, max_bytes=max(att_size, max_size, 1)
                )
                is_text = is_known_text or (
                    not is_media
                    and _is_text_attachment(attachment.filename, content_type, blob)
                )
                if not is_media and not is_text:
                    continue
                mime = (
                    content_type.split(";")[0]
                    if content_type
                    else MIME_MAP.get(
                        ext, "text/plain" if is_text else "application/octet-stream"
                    )
                )
                if not mime.startswith(("image/", "video/", "audio/", "text/")):
                    mime = MIME_MAP.get(ext, mime)
                filename = attachment.filename
                # Respect process_audio (the audio-input toggle) — skip pure audio attachments
                # if disabled. Video may still yield image frames even if audio track skipped later.
                if mime.startswith("audio/") and not proc_aud:
                    continue
                if mime.startswith("image/") and not proc_img:
                    continue
                if mime == "image/gif" or ext == ".gif":
                    normalized = await self._normalize_gif(
                        blob, attachment.filename, max_size
                    )
                    if normalized:
                        blob, mime, filename = normalized
                if mime.startswith("video/"):
                    # Do not leak raw video_url parts when video input is
                    # disabled; providers reject those on the wire.
                    if not parse_bool(
                        getattr(self.config, "ENABLE_VIDEO_INPUT", True), True
                    ):
                        continue
                    normalized = await self._normalize_video(
                        blob, attachment.filename, max_size
                    )
                    if normalized:
                        blob, mime, filename = normalized
                    derived = await self._extract_video_derivatives(
                        blob,
                        filename,
                        getattr(message, "id", None),
                        max_size,
                        source_url=getattr(attachment, "url", "") or "",
                        include_frames=proc_img,
                    )
                    for derived_item in derived:
                        if derived_item.get("is_image"):
                            images.append(derived_item["b64"])
                        media.append(derived_item)
                    # Providers reject raw video_url parts; derivatives are
                    # the complete provider-safe representation.
                    continue
                is_image = ext in image_exts or mime.startswith("image/")
                text = ""
                b64 = ""
                if is_text and not is_image:
                    text = _decode_readable_text(blob)
                    if not text and not is_media:
                        continue
                else:
                    b64 = base64.b64encode(blob).decode("utf-8")
                if is_image:
                    images.append(b64)
                item = self._media_item(
                    b64=b64,
                    mime_type=mime,
                    filename=filename,
                    is_image=is_image,
                    is_text=bool(text),
                    text=text,
                    message_id=getattr(message, "id", None),
                    # Every piece of media carries its source URL when
                    # possible so the model can curl/pull/reuse it in
                    # sites instead of being blind to where it came from.
                    url=getattr(attachment, "url", "") or "",
                )
                if id(attachment) not in wrapper_att_ids:
                    item["source"] = "forward"
                media.append(item)
                kind = "text" if text else "media"
                logger.info(
                    f"Extracted {kind} attachment {filename} ({len(blob)} bytes, mime={mime})"
                )
            except Exception as e:
                logger.error(
                    f"Failed to download attachment {attachment.filename}: {e}"
                )
        if proc_img:
            for source in iter_message_payloads(message):
                for item in await self._extract_sticker_emoji_media(source, max_size):
                    item["message_id"] = getattr(message, "id", None)
                    if source is not message:
                        item["source"] = item.get("source") or "forward"
                    if item.get("is_image") and item.get("b64"):
                        images.append(item["b64"])
                    media.append(item)
        return images, media

    def _max_media_bytes(self) -> int:
        max_mb = float(self._control.get("max_image_size_mb", 10) or 10)
        return _safe_int(max(1, min(max_mb, 25)) * 1024 * 1024, 1048576)

    @staticmethod
    def _ffmpeg_input_argv(input_path) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
        ]

    @staticmethod
    def _media_item(
        *,
        b64: str,
        mime_type: str,
        filename: str,
        is_image: bool,
        message_id,
        url: str | None = None,
        is_text: bool = False,
        text: str = "",
        source: str | None = None,
    ) -> dict:
        item = {
            "b64": b64,
            "mime_type": mime_type,
            "filename": filename,
            "is_image": is_image,
            "is_text": is_text,
            "text": text,
            "message_id": message_id,
        }
        if source is not None:
            item["source"] = source
        if url is not None:
            item["url"] = url
        return item

    async def _normalize_video(
        self, blob: bytes, filename: str, max_size: int
    ) -> tuple[bytes, str, str] | None:
        suffix = Path(filename).suffix.lower() or ".mp4"
        try:
            with tempfile.TemporaryDirectory(prefix="maxwell-video-") as tmp:
                tmp_path = Path(tmp)
                input_path = tmp_path / f"input{suffix}"
                output_path = tmp_path / "normalized.mp4"
                input_path.write_bytes(blob)
                cmd = [
                    *self._ffmpeg_input_argv(input_path),
                    "-vf",
                    "scale='min(1280,iw)':-2,fps=24,format=yuv420p",
                    "-c:v",
                    "libx264",
                    "-profile:v",
                    "baseline",
                    "-level",
                    "3.1",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                try:
                    _stdout, stderr = await communicate_process(
                        proc, timeout=60
                    )
                except asyncio.TimeoutError as _exc:
                    logger.warning(f"Video normalization timed out for {filename}")
                    return None
                if proc.returncode != 0 or not output_path.exists():
                    logger.warning(
                        f"Video normalization failed for {filename}: {stderr.decode(errors='replace')[-300:]}"
                    )
                    return None
                normalized = output_path.read_bytes()
                if len(normalized) > max_size:
                    logger.warning(
                        f"Skipping normalized video {filename}: too large ({len(normalized)} bytes)"
                    )
                    return None
                out_name = f"{Path(filename).stem}-normalized.mp4"
                logger.info(
                    f"Normalized video {filename} -> {out_name} ({len(blob)} -> {len(normalized)} bytes)"
                )
                return normalized, "video/mp4", out_name
        except Exception as e:
            logger.warning(f"Failed to normalize video {filename}: {e}")
            return None

    async def _extract_video_derivatives(
        self,
        blob: bytes,
        filename: str,
        message_id,
        max_size: int,
        source_url: str = "",
        *,
        include_frames: bool | None = None,
        source_prefix: str = "",
    ) -> list[dict]:
        """Extract representative frames and audio track from video for reliable model coverage."""
        results = []
        if include_frames is None:
            include_frames = MaxwellBot._image_input_enabled(self)
        suffix = Path(filename).suffix.lower() or ".mp4"
        try:
            with tempfile.TemporaryDirectory(prefix="maxwell-vderiv-") as tmp:
                tmp_path = Path(tmp)
                video_path = tmp_path / f"input{suffix}"
                video_path.write_bytes(blob)

                # Extract frames at 2fps, capped at 6 frames max and 15s duration
                if include_frames:
                    frame_pattern = str(tmp_path / "frame-%03d.jpg")
                    frame_cmd = [
                        *self._ffmpeg_input_argv(video_path),
                        "-t",
                        "15",
                        "-vf",
                        "fps=2,scale='min(768,iw)':-2",
                        "-frames:v",
                        "6",
                        frame_pattern,
                    ]
                    proc = await asyncio.create_subprocess_exec(
                        *frame_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _stdout, stderr = await communicate_process(
                            proc, timeout=30
                        )
                    except asyncio.TimeoutError as _exc:
                        logger.warning(
                            f"Video frame extraction timed out for {filename}"
                        )
                        stderr = b"timeout"
                    if proc.returncode == 0:
                        for frame_path in sorted(tmp_path.glob("frame-*.jpg")):
                            frame_blob = frame_path.read_bytes()
                            if len(frame_blob) > max_size:
                                continue
                            results.append(
                                self._media_item(
                                    b64=base64.b64encode(frame_blob).decode("utf-8"),
                                    mime_type="image/jpeg",
                                    filename=f"{filename}-{frame_path.stem}.jpg",
                                    is_image=True,
                                    message_id=message_id,
                                    source=(
                                        f"{source_prefix}_frame"
                                        if source_prefix
                                        else "video_frame"
                                    ),
                                    url=source_url,
                                )
                            )
                    else:
                        logger.warning(
                            f"Video frame extraction failed for {filename}: {stderr.decode(errors='replace')[-300:]}"
                        )

                # Extract audio track only if process_audio (omni audio input) is enabled.
                # This prevents sending audio to non-omni or when user disabled audio models.
                proc_aud = _owner_audio_input_enabled(self)
                if proc_aud:
                    audio_path = tmp_path / "audio.wav"
                    audio_cmd = [
                        *self._ffmpeg_input_argv(video_path),
                        "-t",
                        "30",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        str(audio_path),
                    ]
                    proc = await asyncio.create_subprocess_exec(
                        *audio_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _stdout, stderr = await communicate_process(
                            proc, timeout=30
                        )
                    except asyncio.TimeoutError as _exc:
                        logger.info(f"Video audio extraction timed out for {filename}")
                        stderr = b"timeout"
                    if (
                        proc.returncode == 0
                        and audio_path.exists()
                        and audio_path.stat().st_size > 44
                    ):
                        audio_blob = audio_path.read_bytes()
                        if len(audio_blob) <= max_size:
                            results.append(
                                self._media_item(
                                    b64=base64.b64encode(audio_blob).decode("utf-8"),
                                    mime_type="audio/wav",
                                    filename=f"{filename}-audio.wav",
                                    is_image=False,
                                    message_id=message_id,
                                    source=(
                                        f"{source_prefix}_audio"
                                        if source_prefix
                                        else "video_audio"
                                    ),
                                    url=source_url,
                                )
                            )
                    elif proc.returncode != 0:
                        logger.info(
                            f"No extractable audio track for {filename}: {stderr.decode(errors='replace')[-200:]}"
                        )
        except Exception as e:
            logger.warning(f"Failed to derive frames/audio from video {filename}: {e}")
        if results:
            frame_count = sum(1 for item in results if item.get("is_image"))
            audio_count = sum(
                1 for item in results if item.get("mime_type") == "audio/wav"
            )
            logger.info(
                f"Derived {frame_count} frame(s) and {audio_count} audio track(s) from video {filename}"
            )
        return results

    async def _normalize_gif(
        self, blob: bytes, filename: str, max_size: int
    ) -> tuple[bytes, str, str] | None:
        try:
            with tempfile.TemporaryDirectory(prefix="maxwell-gif-") as tmp:
                tmp_path = Path(tmp)
                suffix = Path(filename).suffix.lower()
                if suffix not in {".gif", ".mp4", ".webm", ".webp"}:
                    suffix = ".gif"
                input_path = tmp_path / f"input{suffix}"
                output_path = tmp_path / "gif-sheet.jpg"
                input_path.write_bytes(blob)
                cmd = [
                    *self._ffmpeg_input_argv(input_path),
                    "-vf",
                    "fps=2,scale=320:-2:flags=lanczos,tile=4x2:padding=4:margin=4:color=white",
                    "-frames:v",
                    "1",
                    str(output_path),
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                try:
                    _stdout, stderr = await communicate_process(
                        proc, timeout=30
                    )
                except asyncio.TimeoutError as _exc:
                    logger.warning(f"GIF normalization timed out for {filename}")
                    return None
                if proc.returncode != 0 or not output_path.exists():
                    logger.warning(
                        f"GIF normalization failed for {filename}: {stderr.decode(errors='replace')[-300:]}"
                    )
                    return None
                normalized = output_path.read_bytes()
                if len(normalized) > max_size:
                    logger.warning(
                        f"Skipping normalized GIF {filename}: too large ({len(normalized)} bytes)"
                    )
                    return None
                out_name = f"{Path(filename).stem}-gif-sheet.jpg"
                logger.info(
                    f"Normalized GIF {filename} -> {out_name} ({len(blob)} -> {len(normalized)} bytes)"
                )
                return normalized, "image/jpeg", out_name
        except Exception as e:
            logger.warning(f"Failed to normalize GIF {filename}: {e}")
            return None

    @staticmethod
    def _embed_text(embed) -> str:
        lines = []
        if getattr(embed, "title", None):
            lines.append(f"Title: {embed.title}")
        if getattr(embed, "description", None):
            lines.append(f"Description: {embed.description}")
        if getattr(embed, "url", None):
            lines.append(f"URL: {embed.url}")
        author = getattr(embed, "author", None)
        if author and getattr(author, "name", None):
            author_line = f"Author: {author.name}"
            if getattr(author, "url", None):
                author_line += f" ({author.url})"
            lines.append(author_line)
        provider = getattr(embed, "provider", None)
        if provider and getattr(provider, "name", None):
            lines.append(f"Provider: {provider.name}")
        for field in getattr(embed, "fields", []) or []:
            name = getattr(field, "name", "field")
            value = getattr(field, "value", "")
            if name or value:
                lines.append(f"Field - {name}: {value}")
        footer = getattr(embed, "footer", None)
        if footer and getattr(footer, "text", None):
            lines.append(f"Footer: {footer.text}")
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _embed_media_urls(embed) -> list[tuple[str, str]]:
        urls = []
        for label, obj_name in (
            ("image", "image"),
            ("thumbnail", "thumbnail"),
            ("video", "video"),
        ):
            obj = getattr(embed, obj_name, None)
            url = getattr(obj, "url", None) or getattr(obj, "proxy_url", None)
            if url:
                urls.append((label, str(url)))
        author = getattr(embed, "author", None)
        if author and getattr(author, "icon_url", None):
            urls.append(("author_icon", str(author.icon_url)))
        footer = getattr(embed, "footer", None)
        if footer and getattr(footer, "icon_url", None):
            urls.append(("footer_icon", str(footer.icon_url)))
        seen = set()
        unique = []
        for label, url in urls:
            if url in seen:
                continue
            seen.add(url)
            unique.append((label, url))
        return unique

    # ---- server emoji/sticker reference grid -------------------------------
    # Maxwell was told the *names* of every server emoji/sticker but had never
    # seen one, so it picked them blind. This renders a labeled contact sheet
    # once per guild (cached to disk, keyed by the emoji set) and shows it the
    # first time Maxwell speaks in a channel.
    _GRID_CELL = 72  # icon box, px
    _GRID_LABEL_H = 14  # label strip under each icon, px
    _GRID_COLS = 8
    _GRID_MAX_EMOJIS = 48
    _GRID_MAX_STICKERS = 12

    def _emoji_grid_dir(self) -> Path:
        d = Path(getattr(self.config, "DATA_DIR", "data")) / "emoji_grids"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _grid_entries(self, guild) -> list[tuple[str, str]]:
        """Current-server, application and official icons; no other guilds."""
        from discord_expressions import available_emojis, available_stickers, emoji_image_url
        entries = []
        for name, sticker in sorted(available_stickers(self, guild).items())[:self._GRID_MAX_STICKERS]:
            fmt = getattr(sticker, "format", None)
            if "lottie" in str(getattr(fmt, "name", fmt) or "").lower():
                continue
            if url := str(getattr(sticker, "url", "") or ""):
                entries.append((name, url))
        for name, markup in sorted(available_emojis(self, guild).items())[:self._GRID_MAX_EMOJIS]:
            match = re.fullmatch(r"<(a?):[^:]+:(\d+)>", markup)
            if match:
                entries.append((name, emoji_image_url(match.group(2))))
        return entries

    async def _emoji_grid_media(self, guild) -> dict | None:
        """Build (or load from cache) the labeled grid for a guild."""
        entries = self._grid_entries(guild)
        if not entries:
            return None
        # Key on the exact icon set, so adding or removing an emoji rebuilds
        # rather than serving a stale sheet forever.
        key = hashlib.sha256(
            "|".join(f"{n}:{u}" for n, u in entries).encode("utf-8")
        ).hexdigest()[:16]
        path = self._emoji_grid_dir() / f"{getattr(guild, 'id', 'guild')}-{key}.png"

        if not path.exists():
            built = await asyncio.to_thread(
                self._render_emoji_grid, await self._fetch_grid_icons(entries), path
            )
            if not built:
                return None
        try:
            blob = path.read_bytes()
        except OSError as e:
            logger.warning(f"Emoji grid unreadable ({path}): {e}")
            return None
        return self._media_item(
            b64=base64.b64encode(blob).decode("utf-8"),
            mime_type="image/png",
            filename="SERVER-EMOJI-STICKER-REFERENCE-GRID.png",
            is_image=True,
            message_id=None,
            source="emoji_grid",
        )

    async def _maybe_emoji_grid(self, message, channel_id: str) -> dict | None:
        """Return the grid when this turn is actually about emoji/stickers.

        Attaching it every turn would bill vision tokens on every message in
        every guild. The text name-list is enough unless they ask.
        """
        guild = getattr(message, "guild", None)
        if not self._control.get("emoji_context_enabled", True):
            return None
        asked = str(getattr(message, "content", "") or "")
        if not re.search(
            r"(?i)\b(emoji|sticker|emote|emojis|stickers)\b|:[\w+]{2,}:", asked
        ):
            return None
        try:
            item = await self._emoji_grid_media(guild)
        except Exception as e:
            logger.warning(
                f"Emoji grid build failed for guild {getattr(guild, 'id', '?')}: {e}"
            )
            return None
        if item is None:
            return None
        key = hashlib.sha256(item.get("b64", "").encode()).hexdigest()
        if self._emoji_grid_shown.get(channel_id) == key:
            return None
        self._emoji_grid_shown[channel_id] = key
        logger.info(f"Attaching server emoji grid to channel {channel_id}")
        return item

    async def _fetch_grid_icons(
        self, entries: list[tuple[str, str]]
    ) -> list[tuple[str, bytes]]:
        """Download the icons concurrently; skip whatever fails."""
        sem = asyncio.Semaphore(8)
        session = await _get_shared_session()

        async def one(name: str, url: str):
            async with sem:
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15, connect=6)
                    ) as resp:
                        if resp.status != 200:
                            return None
                        return name, await _read_response_limited(resp, 2 * 1024 * 1024)
                except Exception:
                    return None

        done = await asyncio.gather(*(one(n, u) for n, u in entries))
        return [d for d in done if d]

    @classmethod
    def _render_emoji_grid(cls, icons: list[tuple[str, bytes]], path: Path) -> bool:
        """Compose the contact sheet. Runs in a thread — PIL is blocking."""
        if not icons:
            return False
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            logger.warning("Pillow missing — cannot build emoji grid")
            return False
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10
            )
        except Exception:
            font = ImageFont.load_default()

        cell_h = cls._GRID_CELL + cls._GRID_LABEL_H
        cols = min(cls._GRID_COLS, len(icons))
        rows = (len(icons) + cols - 1) // cols
        # Light background: these are mostly dark-outlined emojis, and a
        # transparent sheet flattens to black in most vision pipelines.
        sheet = Image.new(
            "RGB", (cols * cls._GRID_CELL, rows * cell_h), (245, 245, 247)
        )
        draw = ImageDraw.Draw(sheet)

        for idx, (name, blob) in enumerate(icons):
            col, row = idx % cols, idx // cols
            x0, y0 = col * cls._GRID_CELL, row * cell_h
            try:
                im = Image.open(io.BytesIO(blob))
                im.seek(0)  # animated: first frame is enough to identify it
                im = im.convert("RGBA")
                im.thumbnail((cls._GRID_CELL - 8, cls._GRID_CELL - 8))
                sheet.paste(
                    im,
                    (
                        x0 + (cls._GRID_CELL - im.width) // 2,
                        y0 + (cls._GRID_CELL - im.height) // 2,
                    ),
                    im,
                )
            except Exception as e:
                # One undecodable emoji shouldn't blank the whole sheet.
                logger.debug("Emoji sheet: skipping %s: %s", name, e)
                continue
            label = name if len(name) <= 12 else name[:11] + "…"
            try:
                tw = draw.textlength(label, font=font)
            except Exception:
                tw = len(label) * 5
            draw.text(
                (x0 + max(0, (cls._GRID_CELL - tw) // 2), y0 + cls._GRID_CELL),
                label,
                fill=(30, 30, 35),
                font=font,
            )
        try:
            sheet.save(path, format="PNG", optimize=True)
        except OSError as e:
            logger.warning(f"Failed to save emoji grid {path}: {e}")
            return False
        logger.info(f"Built emoji grid {path.name} ({len(icons)} icons)")
        return True

    # Discord CDN roots for the two "image-like" payloads that are NOT
    # attachments: custom emojis are inline markup in content, stickers ride
    # on message.stickers. Without this the model is blind to both — a
    # sticker-only message has empty content and no attachments, so it used
    # to reach the prompt as an empty string.
    _EMOJI_MARKUP_RE = re.compile(r"<(a)?:([A-Za-z0-9_]{2,32}):(\d{15,25})>")
    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
    _MAX_INLINE_EMOJIS = 3
    _MAX_INLINE_STICKERS = 3

    async def _extract_sticker_emoji_media(self, message, max_size: int) -> list[dict]:
        """Download custom stickers/emojis on a message as real image media.

        Both are plain CDN images, so they go through the same download path
        as embed media (SSRF check, size cap, GIF normalization). Lottie
        stickers are skipped: they are vector JSON, not an image the model
        can look at — the text annotation still names them.
        """
        media: list[dict] = []
        message_id = getattr(message, "id", None)

        for sticker in (getattr(message, "stickers", None) or [])[
            : self._MAX_INLINE_STICKERS
        ]:
            fmt = getattr(sticker, "format", None)
            fmt_name = str(getattr(fmt, "name", fmt) or "").lower()
            if "lottie" in fmt_name:
                logger.info(
                    f"Skipping lottie sticker {getattr(sticker, 'name', '?')} (vector JSON, not an image)"
                )
                continue
            url = str(getattr(sticker, "url", "") or "")
            if not url:
                continue
            name = self._SAFE_NAME_RE.sub(
                "_", str(getattr(sticker, "name", "") or "sticker")
            )[:64]
            ext = Path(urlparse(url).path).suffix.lower() or ".png"
            item = await self._download_embed_media(
                url, f"sticker-{name}{ext}", max_size, message_id
            )
            if item:
                item["source"] = "sticker"
                media.append(item)

        seen: set[str] = set()
        for match in self._EMOJI_MARKUP_RE.finditer(
            str(getattr(message, "content", "") or "")
        ):
            if len(seen) >= self._MAX_INLINE_EMOJIS:
                break
            animated, name, emoji_id = match.groups()
            if emoji_id in seen:
                continue
            seen.add(emoji_id)
            ext = ".gif" if animated else ".png"
            item = await self._download_embed_media(
                f"https://cdn.discordapp.com/emojis/{emoji_id}{ext}",
                f"emoji-{self._SAFE_NAME_RE.sub('_', name)[:64]}{ext}",
                max_size,
                message_id,
            )
            if item:
                item["source"] = "emoji"
                media.append(item)

        return media

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
    _MAX_MEDIA_REDIRECTS = 3
    _META_TAG_RE = re.compile(r"<meta\s+[^>]*>", re.I)
    _META_ATTR_RE = re.compile(r"""([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")

    @staticmethod
    def _is_gif_page_url(url: str) -> bool:
        return is_gif_page_url(url)

    @classmethod
    def _og_media_urls(cls, html_text: str) -> list[str]:
        """og:image / og:video (and twitter: equivalents) in document order."""
        images: list[str] = []
        videos: list[str] = []
        for tag in cls._META_TAG_RE.findall(html_text or ""):
            attrs: dict[str, str] = {}
            for match in cls._META_ATTR_RE.finditer(tag):
                attrs[match.group(1).lower()] = (
                    match.group(2) or match.group(3) or match.group(4) or ""
                )
            prop = (attrs.get("property") or attrs.get("name") or "").lower()
            content = html.unescape(attrs.get("content") or "").strip()
            if not content:
                continue
            if prop in {
                "og:image",
                "og:image:url",
                "twitter:image",
                "twitter:image:src",
            }:
                images.append(content)
            elif prop in {"og:video", "og:video:url", "twitter:player:stream"}:
                videos.append(content)
        return images + videos

    async def _fetch_public_payload(
        self, url: str, max_size: int
    ) -> tuple[str, str, bytes] | None:
        """GET a public URL, following a few SSRF-checked redirects.

        Returns (final_url, mime, blob) or None. Each hop is re-checked with
        _is_safe_url so a public page cannot bounce us onto link-local metadata.
        """
        current = clean_media_url(url)
        refreshed_once = False
        redirects = 0
        try:
            session = await _get_shared_session()
            while redirects <= self._MAX_MEDIA_REDIRECTS:
                if not _is_safe_url(current):
                    logger.warning(f"Skipping unsafe embed media URL: {current[:120]}")
                    return None
                async with session.get(
                    current,
                    timeout=aiohttp.ClientTimeout(total=20, connect=8),
                    allow_redirects=False,
                    headers={
                        "User-Agent": _IMAGE_FETCH_UA,
                        "Accept": "image/*,video/*,text/html;q=0.8,*/*;q=0.5",
                    },
                ) as resp:
                    if resp.status in self._REDIRECT_STATUSES:
                        loc = resp.headers.get("Location")
                        if not loc:
                            logger.warning(
                                f"Skipping embed media {current[:120]}: "
                                "redirect with no Location"
                            )
                            return None
                        current = urljoin(current, loc)
                        redirects += 1
                        continue
                    if resp.status in {403, 404} and not refreshed_once and discord_attachment_key(current):
                        refreshed_once = True
                        refreshed = await refresh_discord_media_url(self, _current_inbound.get(), current)
                        if refreshed and refreshed != current:
                            current = refreshed
                            continue
                    if resp.status != 200:
                        logger.warning(
                            f"Skipping embed media {current[:120]}: HTTP {resp.status}"
                        )
                        return None
                    content_type = (
                        (resp.headers.get("Content-Type") or "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    blob = await _read_response_limited(resp, max_size)
                    return current, content_type, blob
            logger.warning(f"Skipping embed media {url[:120]}: too many redirects")
            return None
        except Exception as e:
            logger.warning(f"Failed to download embed media {url[:120]}: {e}")
            return None

    async def _download_embed_media(
        self, url: str, filename: str, max_size: int, message_id, *, _depth: int = 0
    ) -> dict | None:
        url = clean_media_url(url)
        if _depth > 2:
            return None
        if not _is_safe_url(url):
            logger.warning(f"Skipping unsafe embed media URL: {url[:120]}")
            return None
        fetched = await self._fetch_public_payload(url, max_size)
        if not fetched:
            return None
        final_url, mime, blob = fetched
        ext = (
            Path(urlparse(final_url).path).suffix.lower()
            or Path(urlparse(url).path).suffix.lower()
        )
        if not mime:
            mime = MIME_MAP.get(ext, "")
        host = (urlparse(final_url).hostname or urlparse(url).hostname or "").lower()
        gif_host = (
            self._is_gif_page_url(url)
            or self._is_gif_page_url(final_url)
            or any(
                host == h or host.endswith("." + h)
                for h in ("tenor.com", "giphy.com", "gph.is", "klipy.com")
            )
        )
        if mime.startswith("text/html") or (mime.startswith("text/") and gif_host):
            if _depth >= 2 or not gif_host:
                logger.warning(
                    f"Skipping embed media {url[:120]}: unsupported mime "
                    f"{mime or 'unknown'}"
                )
                return None
            html_text = blob.decode("utf-8", errors="replace")
            for candidate in self._og_media_urls(html_text):
                abs_url = urljoin(final_url, candidate)
                if not _is_safe_url(abs_url) or abs_url in {url, final_url}:
                    continue
                item = await self._download_embed_media(
                    abs_url, filename, max_size, message_id, _depth=_depth + 1
                )
                if item:
                    # Keep the URL the user posted, not the resolved CDN hop.
                    item["url"] = url
                    return item
            logger.warning(f"Skipping gif page {url[:120]}: no usable og:image")
            return None
        if not mime:
            mime = MIME_MAP.get(ext, "application/octet-stream")
        if not mime.startswith(("image/", "video/", "audio/")):
            mime = MIME_MAP.get(ext, mime)
        if not mime.startswith(("image/", "video/", "audio/")):
            logger.warning(
                f"Skipping embed media {url[:120]}: unsupported mime {mime or 'unknown'}"
            )
            return None
        anim_name = filename if Path(filename).suffix else f"{filename}{ext or '.gif'}"
        if mime == "image/gif" or ext == ".gif":
            normalized = await self._normalize_gif(blob, anim_name, max_size)
            if normalized:
                blob, mime, filename = normalized
        elif mime.startswith("video/") and gif_host:
            normalized = await self._normalize_gif(
                blob,
                filename if Path(filename).suffix else f"{filename}.mp4",
                max_size,
            )
            if normalized:
                blob, mime, filename = normalized
        is_image = mime.startswith("image/")
        logger.info(
            f"Extracted embed media {filename} ({len(blob)} bytes, mime={mime})"
        )
        return self._media_item(
            b64=base64.b64encode(blob).decode("utf-8"),
            mime_type=mime,
            filename=filename,
            is_image=is_image,
            message_id=message_id,
            source="embed",
            # Attach the source URL so the model can curl/reuse the
            # original instead of only having a base64 copy.
            url=url,
        )

    async def _extract_video_item_derivatives(
        self,
        item: dict,
        max_size: int,
        *,
        source_prefix: str,
    ) -> list[dict]:
        """Turn a downloaded video item into provider-safe frames/audio.

        Providers used by Maxwell intentionally do not receive ``video_url``
        parts. Keep that incompatibility at the ingestion boundary: linked
        videos and embed videos are decoded with ffmpeg and only their
        representative JPEG frames / optional audio track continue downstream.
        """
        if not isinstance(item, dict):
            return []
        encoded = str(item.get("b64") or "")
        if not encoded:
            return []
        if not parse_bool(
            getattr(getattr(self, "config", None), "ENABLE_VIDEO_INPUT", True),
            True,
        ):
            return []
        try:
            blob = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            logger.warning(
                "Skipping invalid base64 video item %s",
                item.get("filename", "video"),
            )
            return []
        if not blob:
            return []
        return await self._extract_video_derivatives(
            blob,
            str(item.get("filename") or "video.mp4"),
            item.get("message_id"),
            max_size,
            source_url=str(item.get("url") or ""),
            include_frames=MaxwellBot._image_input_enabled(self),
            source_prefix=source_prefix,
        )

    async def _extract_embeds(self, message) -> list[dict]:
        embeds = self._payload_attr_list(message, "embeds", 8)
        if not embeds:
            return []
        max_size = self._max_media_bytes()
        proc_img = MaxwellBot._image_input_enabled(self)
        proc_aud = _owner_audio_input_enabled(self)
        video_input_enabled = parse_bool(
            getattr(getattr(self, "config", None), "ENABLE_VIDEO_INPUT", True),
            True,
        )
        media = []
        text_blocks = []
        message_id = getattr(message, "id", None)
        media_count = 0

        for idx, embed in enumerate(embeds[:5], 1):
            text = self._embed_text(embed)
            embed_media_urls = self._embed_media_urls(embed)
            if text:
                text_blocks.append(f"Embed {idx}:\n{text}")
            if embed_media_urls:
                text_blocks.append(
                    f"Embed {idx} media URLs:\n"
                    + "\n".join(f"  - {u}" for _, u in embed_media_urls)
                )
            # Skip YouTube embeds — we do not download YouTube media.
            embed_url = getattr(embed, "url", None) or ""
            if _is_youtube_url(embed_url):
                continue
            embed_has_image = False
            pending_video: list[dict] = []
            for label, url in embed_media_urls:
                if media_count >= 5 and not (label == "video" and proc_aud):
                    break
                if _is_youtube_url(url):
                    continue
                if label in {"image", "thumbnail"} and not proc_img:
                    continue
                if label == "video" and (
                    not video_input_enabled or not (proc_img or proc_aud)
                ):
                    continue
                ext = Path(urlparse(url).path).suffix.lower()
                filename = f"embed-{idx}-{label}{ext or ''}"
                item = await self._download_embed_media(
                    url, filename, max_size, message_id
                )
                if not item:
                    continue
                mime = str(item.get("mime_type") or "")
                # A gif/klipy video URL can already have been normalized to a
                # JPEG contact sheet by _download_embed_media. It is still a
                # fallback video visual, not a real embed thumbnail.
                if label == "video":
                    pending_video.append(item)
                    continue
                if mime.startswith("video/"):
                    if not video_input_enabled:
                        continue
                    pending_video.append(item)
                    continue
                if media_count >= 5:
                    continue
                media.append(item)
                media_count += 1
                if label in {"image", "thumbnail"} and mime.startswith("image/"):
                    embed_has_image = True
            # GIF/klipy embeds ship a thumbnail + mp4. Keep the thumbnail when
            # it is a real visual; otherwise derive the video. A provider may
            # reject raw video_url parts, so never pass an un-derived video
            # item downstream.
            if not embed_has_image:
                for item in pending_video:
                    if media_count >= 5 and not proc_aud:
                        break
                    mime = str(item.get("mime_type") or "")
                    if mime.startswith("video/"):
                        derived = await self._extract_video_item_derivatives(
                            item,
                            max_size,
                            source_prefix="embed_video",
                        )
                        for derived_item in derived:
                            if media_count >= 5 and derived_item.get("is_image"):
                                continue
                            if derived_item.get("is_image") and not proc_img:
                                continue
                            if not derived_item.get("is_image") and not proc_aud:
                                continue
                            media.append(derived_item)
                            media_count += 1
                    elif mime.startswith("image/") and proc_img:
                        media.append(item)
                        media_count += 1
        if text_blocks:
            media.insert(
                0,
                self._media_item(
                    b64="",
                    mime_type="text/plain",
                    filename="discord-embeds.txt",
                    is_image=False,
                    is_text=True,
                    text="\n\n".join(text_blocks),
                    message_id=message_id,
                    source="embed",
                ),
            )
            logger.info(f"Extracted text from {len(text_blocks)} embed(s)")
        return media

    # Extensions worth pulling out of a bare link in message text. Videos are
    # fetched too, but _extract_linked_media immediately runs them through
    # ffmpeg; a raw video_url part is rejected by several provider endpoints.
    _LINK_IMAGE_EXTS = frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
            ".avif",
            ".apng",
        }
    )
    _LINK_AUDIO_EXTS = frozenset(
        {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".flac", ".aac", ".wma"}
    )
    _LINK_VIDEO_EXTS = frozenset(
        {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg", ".3gp"}
    )

    @classmethod
    def _media_link_refs(cls, content: str | None) -> list[tuple[str, str]]:
        """(url, ext) for every supported direct media link in message text."""
        refs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for url in media_urls_in_text(content or ""):
            # Do not download YouTube URLs.
            if _is_youtube_url(url):
                continue
            if discord_message_link(url):
                ext = ".discord"
            else:
                try:
                    ext = Path(urlparse(url).path).suffix.lower()
                except ValueError:
                    continue
            if (
                ext != ".discord"
                and ext not in cls._LINK_IMAGE_EXTS
                and ext not in cls._LINK_AUDIO_EXTS
                and ext not in cls._LINK_VIDEO_EXTS
            ):
                # Tenor/Giphy picker links have no file extension.
                if cls._is_gif_page_url(url):
                    ext = ".gif"
                else:
                    continue
            if url in seen:
                continue
            seen.add(url)
            refs.append((url, ext))
        return refs

    async def _extract_linked_media(
        self, message, skip_urls: set[str] | None = None
    ) -> list[dict]:
        """Pull media posted as a bare link instead of an upload.

        Discord only unfurls *some* links into embeds, and never audio ones, so
        a plain image, clip, or video URL in message text was invisible to the
        model. Whatever Discord did unfurl is already covered by
        _extract_embeds; `skip_urls` keeps us from downloading it a second time
        and attaching the same media twice.

        Images, audio, and video are gated separately. Everything goes through
        _download_embed_media, which enforces _is_safe_url (no SSRF into the
        host network), the size cap, and a real content-type check — the
        extension only decides what is worth fetching, never what it is.
        """
        proc_img = MaxwellBot._image_input_enabled(self)
        proc_aud = _owner_audio_input_enabled(self)
        if not proc_img and not proc_aud:
            return []
        skip = skip_urls or set()
        video_exts = getattr(self, "_LINK_VIDEO_EXTS", MaxwellBot._LINK_VIDEO_EXTS)
        video_input_enabled = parse_bool(
            getattr(getattr(self, "config", None), "ENABLE_VIDEO_INPUT", True),
            True,
        )
        wanted = [
            (url, ext)
            for url, ext in self._media_link_refs(
                message_combined_content(message)
                or str(getattr(message, "content", "") or "")
            )
            if url not in skip
            and (
                ext == ".discord"
                or (ext in self._LINK_IMAGE_EXTS and proc_img)
                or (ext in self._LINK_AUDIO_EXTS and proc_aud)
                or (
                    ext in video_exts and video_input_enabled and (proc_img or proc_aud)
                )
            )
        ]
        if not wanted:
            return []
        max_size = self._max_media_bytes()
        media = []
        message_id = getattr(message, "id", None)
        for idx, (url, ext) in enumerate(wanted[:5], 1):
            if ext == ".discord":
                target = await resolve_discord_message(message, url)
                if target is None:
                    continue
                _target_images, target_media = await self._extract_media(target)
                target_media.extend(await self._extract_embeds(target))
                caption = render_discord_context_text(target, str(getattr(target, "content", "") or ""))
                if caption:
                    target_media.append(self._media_item(
                        b64="", mime_type="text/plain", filename="linked-message.txt",
                        is_image=False, is_text=True, text=caption[:8000],
                        message_id=getattr(target, "id", None), source="discord_link", url=url,
                    ))
                media.extend(target_media)
                continue
            item = await self._download_embed_media(
                url, f"linked-media-{idx}{ext}", max_size, message_id
            )
            if item:
                item["url"] = url
                mime = str(item.get("mime_type") or "").lower()
                if mime.startswith("video/"):
                    # The provider layer intentionally drops raw video_url
                    # parts. Keep only ffmpeg derivatives so a bare video
                    # link is as useful as an uploaded video.
                    derived = await self._extract_video_item_derivatives(
                        item,
                        max_size,
                        source_prefix="link_video",
                    )
                    for derived_item in derived:
                        derived_item["url"] = url
                    media.extend(derived)
                else:
                    item["source"] = "link"
                    # The media item already carries url= from
                    # _download_embed_media; keep it explicitly sourced.
                    media.append(item)
        if media:
            logger.info(
                f"Extracted {len(media)} linked media item(s) from message text"
            )
        return media
