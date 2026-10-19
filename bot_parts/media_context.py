"""Media-context caching and summaries for the prompt.

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

class MediaContextMixin:

    def _cache_media_context(self, channel_id: str, media: list[dict]):
        image_media = [item for item in media if item.get("is_image")]
        if not image_media:
            return
        cached = self._media_context.setdefault(channel_id, [])
        for item in image_media:
            # Re-caching the same (message_id, filename) means a re-handled turn,
            # not a new image. Bump uses_left on the existing entry instead of
            # appending a duplicate, otherwise the cap fills with N copies of the
            # newest image and they all expire in lockstep after a couple turns.
            mid = (
                str(item.get("message_id"))
                if item.get("message_id") is not None
                else None
            )
            fname = item.get("filename", "attachment")
            replaced = False
            if mid is not None:
                for existing in cached:
                    if (
                        str(existing.get("message_id")) == mid
                        and existing.get("filename") == fname
                    ):
                        existing["uses_left"] = MEDIA_CONTEXT_USES
                        existing["b64"] = item["b64"]
                        existing["mime_type"] = item["mime_type"]
                        existing["url"] = item.get("url", "")
                        existing["source"] = item.get("source")
                        replaced = True
                        break
            if not replaced:
                cached.append(
                    {
                        "b64": item["b64"],
                        "mime_type": item["mime_type"],
                        "filename": fname,
                        "message_id": mid,
                        "url": item.get("url", ""),
                        # Keep the provenance tag: the emoji/sticker grid is
                        # recognised solely by source == "emoji_grid", and
                        # dropping it made a cached grid re-attach later as a
                        # user image.
                        "source": item.get("source"),
                        # Decremented after each handled message. Do not "clean this up"
                        # back to a big number unless you enjoy haunted image context.
                        "uses_left": MEDIA_CONTEXT_USES,
                    }
                )
        # Enforce cap: keep only the most recent MAX_VISUAL_MEMORY_IMAGES
        if len(cached) > MAX_VISUAL_MEMORY_IMAGES:
            cached = cached[-MAX_VISUAL_MEMORY_IMAGES:]
        self._media_context[channel_id] = cached
        logger.info(
            f"Cached {len(image_media)} image(s) for channel {channel_id}; visual memory={len(self._media_context[channel_id])}"
        )

    def _get_media_context(self, channel_id: str, message_id=None) -> list[dict]:
        active = []
        for item in self._media_context.get(channel_id, []):
            if message_id is not None and str(item.get("message_id")) != str(
                message_id
            ):
                continue
            active.append(
                {
                    "b64": item["b64"],
                    "mime_type": item["mime_type"],
                    "filename": item.get("filename", "attachment"),
                    "message_id": item.get("message_id"),
                    "url": item.get("url", ""),
                    "source": item.get("source"),
                }
            )
        return active

    @staticmethod
    def _should_use_cached_media_context(message, content: str) -> bool:
        """Only attach old images when the latest turn actually points at them."""
        return (
            bool(VISUAL_REFERENCE_RE.search(str(content or "")))
            or MaxwellBot._reply_media_message_id(message) is not None
        )

    @staticmethod
    def _reply_media_message_id(message, snapshots=None):
        reference = getattr(message, "reference", None)
        ref = getattr(reference, "resolved", None) if reference else None
        ref_id = getattr(reference, "message_id", None) if reference else None
        if snapshots:
            ref = snapshots.get(str(ref_id or getattr(ref, "id", "") or ""), ref)
        if ref is None:
            return None
        if getattr(ref, "attachments", None) or getattr(ref, "stickers", None):
            return getattr(ref, "id", None)
        if getattr(ref, "embeds", None):
            return getattr(ref, "id", None)
        # A Tenor/Giphy picker message is often just a page URL — no upload
        # and sometimes no embed yet. Still treat a reply to it as "that gif".
        if MaxwellBot._media_link_refs(getattr(ref, "content", "")):
            return getattr(ref, "id", None)
        if iter_message_snapshots(ref):
            return getattr(ref, "id", None)
        return None

    @staticmethod
    def _should_mix_cached_with_current(content: str) -> bool:
        # A new image plus "look at this" should mean the new image, not every
        # cached meme in the channel. Only mix when the user asks for history.
        return bool(PRIOR_VISUAL_REFERENCE_RE.search(str(content or "")))

    @staticmethod
    def _current_binary_media(media: list[dict]) -> list[dict]:
        return [
            item
            for item in media
            if item.get("b64") and not item.get("is_text") and not item.get("is_image")
        ]

    @staticmethod
    def _format_media_summary(
        current_media: list[dict], active_media: list[dict]
    ) -> str:
        current_images = [
            item
            for item in current_media
            if item.get("is_image") and item.get("source") != "emoji_grid"
        ]
        current_other = [item for item in current_media if not item.get("is_image")]
        # The emoji reference sheet is our own injected context, not something a
        # user posted. Listing it as a normal image made Maxwell either ignore it
        # (the image list says "only discuss when relevant") or describe it back
        # at the channel, so it gets its own labeled block below.
        grid_items = [
            item for item in active_media if item.get("source") == "emoji_grid"
        ]
        active_images = [
            item
            for item in active_media
            if str(item.get("mime_type", "")).startswith("image/")
            and item.get("source") != "emoji_grid"
        ]
        active_non_images = [
            item
            for item in active_media
            if not str(item.get("mime_type", "")).startswith("image/")
        ]
        parts = []

        def _media_line(i, item, default_name, default_mime, label):
            filename = item.get("filename", default_name)
            mime = item.get("mime_type", default_mime)
            url = item.get("url") or ""
            return f"{i}. {filename} ({mime}, {label}){' — ' + url if url else ''}"

        if active_images:
            lines = []
            for i, item in enumerate(active_images, 1):
                filename = item.get("filename", "image")
                label = (
                    "new"
                    if any(
                        item.get("message_id") == cur.get("message_id")
                        and filename == cur.get("filename")
                        for cur in current_images
                    )
                    else "recent"
                )
                lines.append(_media_line(i, item, "image", "image", label))
            parts.append(
                "Images available to inspect, oldest to newest. Only discuss them when relevant to the latest message. Source URL attached for each (curl/pull/reuse in sites if needed):\n"
                + "\n".join(lines)
            )
        if grid_items:
            parts.append(
                "Server emoji/sticker reference sheet is attached as an image "
                "(SERVER-EMOJI-STICKER-REFERENCE-GRID.png): a labeled contact sheet "
                "of this server's custom emojis and stickers, each icon captioned "
                "with its exact name underneath. Stickers come first, then emojis. "
                "This is reference material for you — no user posted it, so never "
                "describe, mention, or react to it. Look at it so you know what each "
                ":name: emoji and [sticker_name] actually depicts, and pick ones "
                "whose picture fits what you mean instead of guessing from the name."
            )
        if active_non_images:
            lines = []
            for i, item in enumerate(active_non_images, 1):
                lines.append(_media_line(i, item, "media", "media", "new"))
            parts.append(
                "Audio/video available to inspect in the multimodal message payload. Use the actual attached media when answering:\n"
                + "\n".join(lines)
            )
        if current_other:
            text_items = [
                item
                for item in current_other
                if item.get("is_text") and item.get("text")
            ]
            for item in text_items:
                filename = item.get("filename", "attachment")
                mime = item.get("mime_type", "text/plain")
                url = item.get("url") or ""
                label = (
                    "Embed text"
                    if item.get("source") == "embed"
                    else "Readable attachment"
                )
                url_note = f"\nSource URL: {url}" if url else ""
                parts.append(
                    f"{label}: {filename} ({mime}). Full contents follow:{url_note}\n"
                    f"```text\n{item.get('text', '')}\n```"
                )
        return "\n".join(parts)

    def _tick_media_context(self, channel_id: str):
        cached = self._media_context.get(channel_id)
        if not cached:
            return
        kept = []
        expired = 0
        for item in cached:
            item["uses_left"] = _safe_int(item.get("uses_left", 0), 0) - 1
            if item["uses_left"] > 0:
                kept.append(item)
            else:
                expired += 1
        if kept:
            self._media_context[channel_id] = kept
        else:
            self._media_context.pop(channel_id, None)
        if expired:
            logger.info(
                f"Expired {expired} cached media item(s) for channel {channel_id}"
            )

    # ---- sleep gate ----
    # The bot can take a 1-60 minute sleep window via the `sleep` tool
    # or the restricted `/sleep` command. While sleeping, the triggering
    # channel gets a single "Max is sleeping, back in Xm" notice
    # (deduped per user) and the LLM dispatch is skipped. Never DM
    # the user about sleep. The wake is automatic when the monotonic
    # deadline passes.

    def _image_input_enabled(self) -> bool:
        """Both switches have to agree before an image reaches the model.

        `ENABLE_IMAGE_INPUT` is the install-level switch (documented in the
        README, reported by doctor.py); `process_images` is the dashboard's
        runtime toggle. Only the second one was ever read, so setting
        `ENABLE_IMAGE_INPUT=false` in .env changed nothing and images kept
        being forwarded — the exact opposite of what someone turning it off
        for cost or privacy reasons expects.
        """
        config = getattr(self, "config", None)
        if config is not None and not getattr(config, "ENABLE_IMAGE_INPUT", True):
            return False
        control = getattr(self, "_control", None) or {}
        return parse_bool(control.get("process_images"), True)
