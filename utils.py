"""Shared utility functions used across Maxwell modules.

Don't duplicate these in other files. Import from here.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Discord mention regexes — single source of truth
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")

# Media URLs (direct file links) found in message text. The bot can't attach
# these as binary media without a fetch, but the LLM MUST at least see that
# the message carries a media URL and what type it is — otherwise a message
# like "look at this" + an imgur link reads as pure text.
MEDIA_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+\.(?:"
    r"png|jpe?g|gif|webp|bmp|tiff?|heic|heif|avif|apng|"
    r"mp4|webm|mov|mkv|avi|m4v|mpeg|mpg|3gp|"
    r"mp3|ogg|oga|opus|wav|flac|m4a|aac|wma"
    r")(?:[?#][A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]*)?",
    re.IGNORECASE,
)

# Discord GIF picker / Tenor / Giphy / imgur .gifv — page URLs, not a file ext.
# These used to reach the model as plain text so it could read the link but
# never see the animation.
GIF_PAGE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"(?:(?:media\d*|c)\.)?tenor\.com/[^\s<>\"']+"
    r"|giphy\.com/(?:gifs|media|embed|clips)/[^\s<>\"']+"
    r"|i\.giphy\.com/[^\s<>\"']+"
    r"|media\d*\.giphy\.com/[^\s<>\"']+"
    r"|gph\.is/[^\s<>\"']+"
    r"(?:(?:media|cdn)\.)?klipy\.com/[^\s<>\"']+"
    r"|i\.imgur\.com/[A-Za-z0-9]+\.gifv"
    r"|imgur\.com/[A-Za-z0-9]+\.gifv"
    r")",
    re.IGNORECASE,
)

_DIRECT_IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".apng", ".gifv"}
)


def is_gif_page_url(url: str) -> bool:
    """True for Tenor/Giphy/klipy/imgur-gifv pages (no .gif required)."""
    return bool(GIF_PAGE_URL_RE.match(str(url or "").strip()))


def is_direct_image_url(url: str) -> bool:
    """True when the path ends in a still/animated image extension."""
    try:
        path = urlparse(str(url or "")).path.lower()
    except Exception:
        return False
    return Path(path).suffix in _DIRECT_IMAGE_EXTS


# Human-readable labels for Discord system message types (welcome messages,
# joins, boosts, pins, stage, incidents, etc.). Ordinary chat types are
# skipped in the annotator. Anything unmapped falls back to the enum name.
_ORDINARY_MESSAGE_TYPE_NAMES = frozenset(
    {
        "default",
        "reply",
        "chat_input_command",
        "context_menu_command",
    }
)

SYSTEM_MESSAGE_LABELS = {
    "MessageType.new_member": "new member joined the server",
    "MessageType.member_join": "member joined the server",
    "MessageType.user_join": "member joined the server",
    "MessageType.guild_stream": "started streaming",
    "MessageType.guild_application_premium_subscription": "booster subscribed",
    "MessageType.premium_guild_subscription": "boosted the server",
    "MessageType.premium_guild_tier_1": "server reached tier 1",
    "MessageType.premium_guild_tier_2": "server reached tier 2",
    "MessageType.premium_guild_tier_3": "server reached tier 3",
    "MessageType.channel_pinned_message": "pinned a message",
    "MessageType.pins_add": "pinned a message",
    "MessageType.recipient_add": "added a user to the group",
    "MessageType.recipient_remove": "removed a user from the group",
    "MessageType.channel_name_change": "renamed the channel",
    "MessageType.channel_icon_change": "changed the channel icon",
    "MessageType.channel_follow_add": "added a channel follower",
    "MessageType.call": "started a call",
    "MessageType.guild_discovery_disqualified": "server was disqualified from discovery",
    "MessageType.guild_discovery_requalified": "server was requalified for discovery",
    "MessageType.guild_discovery_grace_period_initial_warning": "server discovery grace period started",
    "MessageType.guild_discovery_grace_period_final_warning": "server discovery grace period ending",
    "MessageType.guild_invite_reminder": "server invite reminder",
    "MessageType.auto_moderation_action": "auto-moderation action",
    "MessageType.stage_start": "stage started",
    "MessageType.stage_end": "stage ended",
    "MessageType.stage_speaker": "was invited to speak on stage",
    "MessageType.stage_raise_hand": "requested to speak on stage",
    "MessageType.stage_topic": "changed the stage topic",
    "MessageType.role_subscription_purchase": "subscribed to a server role",
    "MessageType.interaction_premium_upsell": "premium upsell interaction",
    "MessageType.purchase_notification": "made a purchase",
    "MessageType.poll_result": "poll ended",
    "MessageType.emoji_added": "added a server emoji",
    "MessageType.thread_created": "started a thread",
    "MessageType.thread_starter_message": "thread starter message",
    "MessageType.guild_incident_alert_mode_enabled": "enabled security actions",
    "MessageType.guild_incident_alert_mode_disabled": "disabled security actions",
    "MessageType.guild_incident_report_raid": "reported a raid",
    "MessageType.guild_incident_report_false_alarm": "reported a false alarm",
}


def _message_type_name(mtype) -> str:
    """Lowercased MessageType name from an enum, string, or None."""
    if mtype is None:
        return ""
    name = str(getattr(mtype, "name", "") or "").strip()
    if name:
        return name.lower()
    text = str(mtype)
    if text.startswith("MessageType."):
        return text.split(".", 1)[-1].lower()
    return text.lower()


def message_is_discord_system_event(message: Any) -> bool:
    """True for Discord-rendered system events (joins, pins, boosts, …)."""
    return _message_type_name(getattr(message, "type", None)) not in (
        "",
        *_ORDINARY_MESSAGE_TYPE_NAMES,
    )


def _poll_text(poll) -> str:
    """Render a discord Poll into a compact context line."""
    q = getattr(poll, "question", None)
    qtext = getattr(q, "text", "") if q is not None else ""
    if not qtext and q is not None:
        qtext = str(q)
    opts = []
    for a in list(getattr(poll, "answers", []) or []):
        at = getattr(a, "text", "")
        if hasattr(at, "text"):
            at = getattr(at, "text", "")
        vc = getattr(a, "vote_count", 0)
        opts.append(f"{str(at or '?')[:120]} ({vc} votes)")
    out = f"[poll: {str(qtext)[:200]}"
    if opts:
        out += " | options: " + "; ".join(opts)
    if getattr(poll, "multiple", False):
        out += " | multiple choice"
    total = getattr(poll, "total_votes", None)
    if total:
        out += f" | total votes: {total}"
    out += "]"
    return out


def _iter_components(root: Any):
    """Walk Discord layout/action-row trees and yield leaf-ish children."""
    stack = []
    comps = getattr(root, "components", None)
    if comps is None and hasattr(root, "children"):
        comps = getattr(root, "children", None)
    if comps is None and isinstance(root, (list, tuple)):
        comps = root
    if comps:
        stack.extend(list(comps))
    seen = 0
    while stack and seen < 40:
        item = stack.pop(0)
        seen += 1
        yield item
        kids = getattr(item, "children", None) or getattr(item, "components", None)
        if kids:
            stack.extend(list(kids))
        accessory = getattr(item, "accessory", None)
        if accessory is not None:
            stack.append(accessory)


def _describe_component(comp: Any) -> str | None:
    """One compact button/select/text line, or None for layout wrappers."""
    options = list(getattr(comp, "options", []) or [])
    placeholder = str(getattr(comp, "placeholder", "") or "").strip()
    if options or placeholder:
        labels = []
        for opt in options[:6]:
            label = str(
                getattr(opt, "label", None) or getattr(opt, "value", None) or ""
            ).strip()
            if label:
                labels.append(label[:40])
        bit = placeholder or "menu"
        if labels:
            bit += ": " + ", ".join(labels)
        return f"select {bit}"

    label = str(getattr(comp, "label", "") or "").strip()
    url = str(getattr(comp, "url", "") or "").strip()
    custom_id = str(getattr(comp, "custom_id", "") or "").strip()
    emoji = getattr(comp, "emoji", None)
    emoji_txt = ""
    if emoji is not None:
        emoji_txt = str(
            getattr(emoji, "name", None) or getattr(emoji, "id", "") or ""
        ).strip()
    if label or url or (custom_id and not getattr(comp, "children", None)):
        name = label or emoji_txt or "button"
        if url:
            return f"link {name} <{url}>"
        return f"button {name}"

    content = str(getattr(comp, "content", "") or "").strip()
    if content:
        return f"text {content[:80]}"
    return None


def _component_annotations(message: Any) -> list[str]:
    bits = []
    seen: set[str] = set()
    for comp in _iter_components(message):
        desc = _describe_component(comp)
        if not desc or desc in seen:
            continue
        seen.add(desc)
        bits.append(desc)
        if len(bits) >= 12:
            break
    if not bits:
        return []
    return ["[components: " + "; ".join(bits) + "]"]


def _attachment_is_voice(att: Any) -> bool:
    checker = getattr(att, "is_voice_message", None)
    if callable(checker):
        with contextlib.suppress(Exception):
            return bool(checker())
    return (
        getattr(att, "duration", None) is not None
        and getattr(att, "waveform", None) is not None
    )


def _attachment_is_clip(att: Any) -> bool:
    flags = getattr(att, "flags", None)
    if flags is not None and bool(getattr(flags, "clip", False)):
        return True
    if getattr(att, "clip_created_at", None) is not None:
        return True
    if getattr(att, "clip_participants", None):
        return True
    return False


def _attachment_annotation(att: Any) -> str:
    name = str(getattr(att, "filename", "") or "file")
    ctype = str(getattr(att, "content_type", "") or "").split(";")[0].lower()
    lower = name.lower()
    extras: list[str] = []
    spoiler = getattr(att, "is_spoiler", None)
    if callable(spoiler):
        with contextlib.suppress(Exception):
            spoiler = spoiler()
    if spoiler:
        extras.append("spoiler")
    width = getattr(att, "width", None)
    height = getattr(att, "height", None)
    if width and height:
        extras.append(f"{int(width)}x{int(height)}")
    title = str(getattr(att, "title", "") or "").strip()
    desc = str(getattr(att, "description", "") or "").strip()
    if title and title.lower() != name.lower():
        extras.append(f'title "{title[:80]}"')
    if desc:
        extras.append(f'alt "{desc[:80]}"')
    if _attachment_is_clip(att):
        extras.append("clip")
        app = getattr(att, "application", None)
        app_name = str(getattr(app, "name", "") or "").strip()
        if app_name:
            extras.append(f"from {app_name}")
        parts = []
        for user in list(getattr(att, "clip_participants", None) or [])[:6]:
            nm = str(
                getattr(user, "display_name", None) or getattr(user, "name", "") or ""
            ).strip()
            if nm:
                parts.append(nm)
        if parts:
            extras.append("with " + ", ".join(parts))
    extra = (" " + " ".join(f"({x})" for x in extras)) if extras else ""
    if _attachment_is_voice(att):
        dur = getattr(att, "duration", None)
        dur_bit = f" {float(dur):.0f}s" if dur else ""
        return f"[voice message:{dur_bit} {name}]{extra}"
    if ctype.startswith("image/") or lower.endswith(
        (
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
        )
    ):
        return f"[image: {name}]{extra}"
    if ctype.startswith("audio/") or lower.endswith(
        (".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".flac", ".aac", ".wma")
    ):
        dur = getattr(att, "duration", None)
        dur_bit = f" {float(dur):.0f}s" if dur else ""
        return f"[audio:{dur_bit} {name}]{extra}"
    if ctype.startswith("video/") or lower.endswith(
        (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg", ".3gp")
    ):
        dur = getattr(att, "duration", None)
        dur_bit = f" {float(dur):.0f}s" if dur else ""
        return f"[video:{dur_bit} {name}]{extra}"
    return f"[file: {name}]{extra}"


def iter_message_snapshots(message: Any) -> list:
    """Discord forwards stash the original payload on snapshots, not attachments.

    discord.py-self exposes ``message_snapshots``; some wrappers use ``snapshots``.
    """
    raw = getattr(message, "message_snapshots", None)
    if raw is None:
        raw = getattr(message, "snapshots", None)
    if not raw:
        return []
    out = []
    seen: set[int] = set()
    for snap in raw:
        if snap is None or snap is message:
            continue
        marker = id(snap)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(snap)
    return out


def iter_message_payloads(message: Any) -> list:
    """The wrapping message plus any forwarded snapshots."""
    return [message, *iter_message_snapshots(message)]


def message_combined_content(message: Any) -> str:
    """Plain text from the wrapper and every forwarded snapshot."""
    parts = []
    for source in iter_message_payloads(message):
        text = str(getattr(source, "content", "") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def message_reference_is_forward(message: Any) -> bool:
    """True for Discord's Forward action (not a reply)."""
    if iter_message_snapshots(message):
        return True
    if getattr(getattr(message, "flags", None), "forwarded", False):
        return True
    ref = getattr(message, "reference", None)
    if ref is None:
        return False
    rtype = getattr(ref, "type", None)
    if rtype is None:
        return False
    name = str(getattr(rtype, "name", "") or "").lower()
    if name == "forward":
        return True
    rendered = str(rtype).lower()
    return rendered.endswith(".forward") or rendered == "forward"


def _forward_origin_label(message: Any) -> str:
    ref = getattr(message, "reference", None)
    if ref is None:
        return ""
    bits = []
    guild_id = getattr(ref, "guild_id", None)
    channel_id = getattr(ref, "channel_id", None)
    message_id = getattr(ref, "message_id", None)
    if guild_id:
        bits.append(f"server {guild_id}")
    if channel_id:
        bits.append(f"#{channel_id}")
    if message_id:
        bits.append(f"id {message_id}")
    return " ".join(bits)


def message_has_visible_payload(message: Any) -> bool:
    """True when a Discord message carries anything Maxwell should remember."""
    if str(getattr(message, "content", "") or "").strip():
        return True
    if list(getattr(message, "attachments", None) or []):
        return True
    if list(getattr(message, "embeds", None) or []):
        return True
    if list(getattr(message, "stickers", None) or []):
        return True
    if list(getattr(message, "components", None) or []):
        return True
    if getattr(message, "poll", None) is not None:
        return True
    if message_reference_is_forward(message):
        return True
    if message_is_discord_system_event(message):
        return True
    if getattr(message, "activity", None) is not None:
        return True
    if getattr(message, "call", None) is not None:
        return True
    if getattr(message, "role_subscription", None) is not None:
        return True
    if getattr(message, "purchase_notification", None) is not None:
        return True
    for snap in iter_message_snapshots(message):
        if message_has_visible_payload(snap):
            return True
    return False


def _system_event_annotation(message: Any) -> str | None:
    """Discord-rendered system/welcome/boost/stage/incident line, or None."""
    mtype = getattr(message, "type", None)
    name = _message_type_name(mtype)
    if not name or name in _ORDINARY_MESSAGE_TYPE_NAMES:
        return None
    sys_text = ""
    with contextlib.suppress(Exception):
        sys_text = str(getattr(message, "system_content", "") or "").strip()
    content = str(getattr(message, "content", "") or "").strip()
    if sys_text and sys_text != content:
        return f"[system: {sys_text}]"
    key = f"MessageType.{name}"
    label = (
        SYSTEM_MESSAGE_LABELS.get(key)
        or SYSTEM_MESSAGE_LABELS.get(str(mtype))
        or name.replace("_", " ")
    )
    author = getattr(message, "author", None)
    aname = getattr(author, "display_name", None) if author is not None else None
    return f"[system: {label}" + (f" — {aname}" if aname else "") + "]"


def _thread_annotation(message: Any) -> str | None:
    channel = getattr(message, "channel", None)
    if channel is None:
        return None
    parent = getattr(channel, "parent", None)
    if parent is None:
        return None
    tname = str(getattr(channel, "name", "") or "").strip()
    if not tname:
        return None
    pname = str(getattr(parent, "name", "") or "").strip()
    if pname:
        return f"[thread: {tname} in #{pname}]"
    return f"[thread: {tname}]"


def _call_annotation(message: Any) -> str | None:
    call = getattr(message, "call", None)
    if call is None:
        return None
    bits = ["call"]
    if getattr(call, "ended_timestamp", None):
        bits.append("ended")
    names = []
    for user in list(getattr(call, "participants", None) or [])[:8]:
        nm = str(
            getattr(user, "display_name", None) or getattr(user, "name", "") or ""
        ).strip()
        if nm:
            names.append(nm)
    if names:
        bits.append("participants: " + ", ".join(names))
    return "[" + " | ".join(bits) + "]"


def _message_invite_activity_annotation(message: Any) -> str | None:
    """Join / spectate / listen-along invite attached to the message."""
    activity = getattr(message, "activity", None)
    if activity is None:
        return None
    if isinstance(activity, dict):
        atype = activity.get("type")
        party = activity.get("party_id") or ""
    else:
        atype = getattr(activity, "type", None)
        party = getattr(activity, "party_id", "") or ""
    type_map = {1: "join", 2: "spectate", 3: "listen", 5: "join request"}
    try:
        label = type_map.get(int(atype), str(atype or "activity"))
    except (TypeError, ValueError):
        label = str(atype or "activity")
    bit = f"[message activity: {label}"
    if party:
        bit += f" party {party}"
    return bit + "]"


def _role_subscription_annotation(message: Any) -> str | None:
    sub = getattr(message, "role_subscription", None)
    if sub is None:
        return None
    bits = ["role subscription"]
    tier = str(getattr(sub, "tier_name", "") or "").strip()
    if tier:
        bits.append(tier)
    if getattr(sub, "is_renewal", False):
        bits.append("renewal")
    months = getattr(sub, "total_months_subscribed", None)
    if months:
        bits.append(f"{months} months")
    return "[" + " — ".join(bits) + "]"


def _purchase_annotation(message: Any) -> str | None:
    note = getattr(message, "purchase_notification", None)
    if note is None:
        return None
    gp = getattr(note, "guild_product_purchase", None)
    name = (
        str(getattr(gp, "product_name", "") or "").strip() if gp is not None else ""
    )
    if name:
        return f"[purchase: {name}]"
    return "[purchase notification]"


def _message_flags_annotation(message: Any) -> str | None:
    flags = getattr(message, "flags", None)
    if flags is None:
        return None
    bits: list[str] = []
    seen: set[str] = set()
    for attr, label in (
        ("voice", "voice message"),
        ("is_voice_message", "voice message"),
        ("silent", "silent"),
        ("suppress_notifications", "silent"),
        ("urgent", "urgent"),
        ("crossposted", "crossposted"),
        ("is_crossposted", "crossposted"),
        ("ephemeral", "ephemeral"),
        ("source_message_deleted", "source deleted"),
        ("suppress_embeds", "embeds suppressed"),
        ("has_thread", "has thread"),
    ):
        if bool(getattr(flags, attr, False)) and label not in seen:
            seen.add(label)
            bits.append(label)
    if not bits:
        return None
    return "[flags: " + ", ".join(bits) + "]"


def _render_message_annotations(message: Any, raw_content: str = "") -> str:
    """Extra structured context Discord messages carry outside plain content:
    polls, app commands, system events, embeds, attachments, buttons/selects,
    and direct media URLs. Returns annotation lines (joined), or ''.
    """
    parts: list[str] = []

    poll = getattr(message, "poll", None)
    if poll is not None:
        try:
            parts.append(_poll_text(poll))
        except Exception as e:
            # A malformed poll must not cost us the rest of the annotation.
            logger.debug("Poll annotation failed: %s", e)

    # Stickers carry no content and are not attachments, so without this a
    # sticker-only message renders as an empty string and the model never
    # learns it happened. The image itself is fetched separately in
    # _extract_sticker_emoji_media; this is the text-side signal.
    stickers = list(getattr(message, "stickers", None) or [])
    if stickers:
        names = []
        for st in stickers[:3]:
            try:
                nm = str(getattr(st, "name", "") or "").strip()
                if not nm:
                    continue
                fmt = str(
                    getattr(getattr(st, "format", None), "name", "") or ""
                ).strip()
                names.append(f"{nm} ({fmt})" if fmt else nm)
            except Exception as e:
                logger.debug("Sticker name unreadable: %s", e)
        if names:
            parts.append("[sticker: " + ", ".join(names) + "]")

    for att in list(getattr(message, "attachments", None) or [])[:5]:
        try:
            parts.append(_attachment_annotation(att))
        except Exception as e:
            logger.debug("Attachment annotation failed: %s", e)
            continue

    parts.extend(_component_annotations(message))

    inter = getattr(message, "interaction", None) or getattr(
        message, "interaction_metadata", None
    )
    if inter is not None:
        try:
            name = getattr(inter, "name", None) or ""
            itype = getattr(inter, "type", None)
            if name:
                parts.append(f"[app command: /{name}]")
            else:
                parts.append(f"[app interaction: {itype}]")
        except Exception as e:
            logger.debug("Interaction annotation failed: %s", e)

    for extra in (
        _system_event_annotation(message),
        _thread_annotation(message),
        _call_annotation(message),
        _message_invite_activity_annotation(message),
        _role_subscription_annotation(message),
        _purchase_annotation(message),
        _message_flags_annotation(message),
    ):
        if extra:
            parts.append(extra)

    embeds = list(getattr(message, "embeds", []) or [])
    for e in embeds[:3]:
        try:
            title = str(getattr(e, "title", None) or "").strip()
            desc = str(getattr(e, "description", None) or "").strip()
            url = str(getattr(e, "url", None) or "").strip()
            ea = getattr(e, "author", None)
            aname = str(getattr(ea, "name", "")) if ea is not None else ""
            fields = []
            for f in list(getattr(e, "fields", []) or [])[:8]:
                try:
                    fn = str(getattr(f, "name", "") or "")
                    fv = str(getattr(f, "value", "") or "")[:160]
                    fields.append(f"{fn}: {fv}")
                except Exception as e:
                    logger.debug("Embed field unreadable: %s", e)
                    continue
            img = getattr(e, "image", None)
            thumb = getattr(e, "thumbnail", None)
            video = getattr(e, "video", None)
            img_url = str(getattr(img, "url", "") or "") if img is not None else ""
            thumb_url = (
                str(getattr(thumb, "url", "") or "") if thumb is not None else ""
            )
            video_url = (
                str(getattr(video, "url", "") or "") if video is not None else ""
            )
            footer = getattr(e, "footer", None)
            footer_text = (
                str(getattr(footer, "text", "") or "").strip()
                if footer is not None
                else ""
            )
            provider = getattr(e, "provider", None)
            provider_name = (
                str(getattr(provider, "name", "") or "").strip()
                if provider is not None
                else ""
            )
            line = "[embed:"
            if title:
                line += f" {title[:200]}"
            if aname:
                line += f" (by {aname})"
            if provider_name:
                line += f" via {provider_name}"
            if desc:
                line += f" — {desc[:400]}"
            if url:
                line += f" <{url}>"
            if fields:
                line += " | " + "; ".join(fields)
            if footer_text:
                line += f" | footer: {footer_text[:120]}"
            if img_url or thumb_url:
                line += f" | image: {img_url or thumb_url}"
            if video_url:
                line += f" | video: {video_url}"
            parts.append(line + "]")
        except Exception as e:
            logger.debug("Embed annotation failed: %s", e)
            continue

    # Direct media URLs inside the message text (imgur/discord CDN/mp4 etc.)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in MEDIA_URL_RE.finditer(raw_content):
        u = m.group(0).rstrip(".,;:!?)]}")
        if u in seen:
            continue
        seen.add(u)
        ext = u.rsplit(".", 1)[-1].split("?")[0].lower() if "." in u else ""
        kind = {
            "png": "image",
            "jpg": "image",
            "jpeg": "image",
            "gif": "image",
            "webp": "image",
            "bmp": "image",
            "tif": "image",
            "tiff": "image",
            "heic": "image",
            "heif": "image",
            "avif": "image",
            "apng": "image",
            "mp4": "video",
            "webm": "video",
            "mov": "video",
            "mkv": "video",
            "avi": "video",
            "m4v": "video",
            "mpeg": "video",
            "mpg": "video",
            "3gp": "video",
            "mp3": "audio",
            "ogg": "audio",
            "oga": "audio",
            "opus": "audio",
            "wav": "audio",
            "flac": "audio",
            "m4a": "audio",
            "aac": "audio",
            "wma": "audio",
        }.get(ext, "file")
        found.append((kind, u))
    for m in GIF_PAGE_URL_RE.finditer(raw_content):
        u = m.group(0).rstrip(".,;:!?)]}")
        if u in seen:
            continue
        seen.add(u)
        found.append(("gif", u))
    for kind, u in found[:5]:
        parts.append(f"[media URL: {kind} {u}]")

    for snap in iter_message_snapshots(message)[:3]:
        origin = _forward_origin_label(message)
        snap_text = str(getattr(snap, "content", "") or "").strip()
        header = "[forwarded message"
        if origin:
            header += f" from {origin}"
        if snap_text:
            header += f": {snap_text[:1500]}"
        header += "]"
        parts.append(header)
        nested = _render_message_annotations(snap, raw_content=snap_text)
        if nested:
            parts.append(nested)

    return "\n".join(parts)


def _atomic_json_write_sync(path: Path, data):
    """Atomic JSON write: temp file -> fsync -> rename.

    Correctly handles fd ownership: os.fdopen takes ownership of the fd,
    so we set fd = -1 afterward to prevent double-close in the finally block.

    2026-07-21: also fsync the parent directory after os.replace. On
    Linux, after a crash between os.replace and the next sync, the
    directory entry for `path` may not be persisted even though the
    inode is on disk. On reboot, the file is "gone" from the
    directory listing — load_from_disk quietly returns {}. That was
    a silent memory-wipe trigger. fsync'ing the parent dir closes
    the gap.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1  # fdopen took ownership — don't double-close
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _atomic_text_write_sync(path: Path, text: str):
    """Atomic text write: temp file -> fsync -> rename.

    Same fd ownership handling as _atomic_json_write_sync. 2026-07-21:
    also fsync the parent directory (see _atomic_json_write_sync).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1  # fdopen took ownership — don't double-close
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _fsync_dir(dir_path: Path) -> None:
    """fsync a directory. Best-effort; not all filesystems support it."""
    try:
        dfd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(dfd)


# asyncio holds only a WEAK reference to a running task, so a detached
# create_task() whose handle nobody keeps can be garbage-collected mid-flight.
# providers.py and tool_progress.py learned this the hard way (dropped progress
# edits); the same pattern kills background loops — a collected
# _daily_summarizer_loop() means LTM summarization just stops until the next
# restart, with nothing in the logs. One process-wide strong-ref set, so a task
# is held until it finishes no matter which module spawned it.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Schedule a detached task that cannot be GC'd before it finishes.

    Raises RuntimeError with no running loop (import-time / sync context) and
    closes the coroutine first, so callers that wrap this in
    contextlib.suppress(RuntimeError) don't leak an un-awaited coroutine.
    """
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        raise
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


def _safe_int(val, default=0):
    """Parse int safely, returning default on failure."""
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_utc_datetime(value) -> datetime | None:
    """Normalize any datetime-like value to UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _discord_display_name(obj: Any) -> str:
    """Get display name from a Discord user/member object."""
    return str(
        getattr(obj, "display_name", None)
        or getattr(obj, "name", None)
        or getattr(obj, "id", "unknown")
    )


def _discord_id(obj: Any) -> str:
    """Get string ID from a Discord object."""
    return str(getattr(obj, "id", "unknown"))


def render_discord_context_text(
    message: Any, content: str | None = None, known_users: dict | None = None
) -> str:
    """Make Discord tokens readable for prompts/logged context without mutating the real message.
    known_users: optional {user_id: display_name} from conversation history to resolve pings.

    Also appends structured annotations for non-text payloads the message
    carries: polls, app-command invocations, system/welcome events, embeds,
    and direct media URLs in the text. This runs on EVERY message context
    build (memory writes, autonomy, live replies), so media/polls/embeds
    from users who never pinged the bot still reach the model's context.
    """
    text = str(
        content if content is not None else (getattr(message, "content", "") or "")
    )
    annotations = _render_message_annotations(message, raw_content=text)
    if not text:
        return annotations

    guild = getattr(message, "guild", None)
    users = {
        _discord_id(user): user for user in list(getattr(message, "mentions", []) or [])
    }
    channels = {
        _discord_id(ch): ch
        for ch in list(getattr(message, "channel_mentions", []) or [])
    }
    roles = {
        _discord_id(role): role
        for role in list(getattr(message, "role_mentions", []) or [])
    }

    def replace_user(match: re.Match) -> str:
        user_id = match.group(1)
        user = users.get(user_id)
        if user is None and guild is not None:
            user = guild.get_member(int(user_id))
        if user is None and known_users and user_id in known_users:
            name = known_users[user_id]
            return f"@{name}({user_id})"
        if user is None:
            return f"@unknown-user({user_id})"
        return f"@{_discord_display_name(user)}({user_id})"

    def replace_channel(match: re.Match) -> str:
        channel_id = match.group(1)
        channel = channels.get(channel_id)
        if channel is None and guild is not None:
            channel = guild.get_channel(int(channel_id))
        if channel is None:
            return f"#unknown-channel({channel_id})"
        return f"#{getattr(channel, 'name', channel_id)}({channel_id})"

    def replace_role(match: re.Match) -> str:
        role_id = match.group(1)
        role = roles.get(role_id)
        if role is None and guild is not None:
            role = guild.get_role(int(role_id))
        if role is None:
            return f"@unknown-role({role_id})"
        return f"@{getattr(role, 'name', role_id)}({role_id})"

    text = USER_MENTION_RE.sub(replace_user, text)
    text = CHANNEL_MENTION_RE.sub(replace_channel, text)
    text = ROLE_MENTION_RE.sub(replace_role, text)

    # Timestamp of the message itself — the model needs to know WHEN each
    # message happened (recency, "yesterday vs now", dead conversations).
    created = getattr(message, "created_at", None)
    if created is not None:
        ts = created.strftime("%Y-%m-%d %H:%M:%S UTC")
        text = f"[at {ts}] {text}" if text else f"[at {ts}]"

    if annotations:
        text = f"{text}\n{annotations}"
    return text


# Alias for autonomy.py compatibility
_render_discord_context_text = render_discord_context_text


def format_reactions_annotation(entries: list | None) -> str:
    """Turn reaction rows into `[reactions: 😂 alice, bob; 👍×2]` for prompts."""
    if not entries:
        return ""
    grouped: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    order: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        emoji = str(item.get("emoji") or "").strip()
        if not emoji:
            continue
        if emoji not in grouped:
            grouped[emoji] = []
            counts[emoji] = 0
            order.append(emoji)
        name = str(item.get("user_name") or item.get("name") or "").strip()
        if name and name not in grouped[emoji]:
            grouped[emoji].append(name)
        try:
            counts[emoji] += max(1, int(item.get("count") or 1))
        except (TypeError, ValueError):
            counts[emoji] += 1
    bits: list[str] = []
    for emoji in order:
        names = grouped[emoji][:12]
        if names:
            bits.append(f"{emoji} {', '.join(names)}")
        elif counts[emoji] > 1:
            bits.append(f"{emoji}×{counts[emoji]}")
        else:
            bits.append(emoji)
    if not bits:
        return ""
    return "[reactions: " + "; ".join(bits) + "]"


# --- Cross-process file locking (Linux fcntl; best-effort elsewhere) ---
# Used to reduce lost-update races on shared JSONs between bot and api processes
# (bot_commands.json, autonomy state, rem state, etc.). Not a full DB, but
# makes the existing read-modify-write pattern much safer.
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore


class FileLockTimeout(TimeoutError):
    """Raised when an exclusive FileLock cannot be acquired within timeout."""


class FileLock:
    """Exclusive file lock using a *sidecar* lock file + fcntl.flock.

    The lock is held on ``{path}.lock``, never on the data file itself. That
    matters because callers use atomic ``os.replace`` on the data path — locking
    the data inode was broken (replace swaps the inode out from under flock).

    On timeout this raises ``FileLockTimeout`` (fail closed) instead of
    proceeding unlocked. Without fcntl it still serializes best-effort via the
    sidecar fd but cannot enforce cross-process exclusion.
    Usage:
        with FileLock(path):
            data = json.loads(path.read_text() or '[]')
            ... mutate ...
            _atomic_json_write_sync(path, data)
    """

    def __init__(
        self, path: Path | str, timeout: float = 30.0, *, fail_open: bool = False
    ):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.timeout = timeout
        self.fail_open = fail_open
        self._fd = None
        self._locked = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        if fcntl is not None:
            import time as _time

            deadline = _time.time() + self.timeout
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._locked = True
                    break
                except BlockingIOError:
                    if _time.time() > deadline:
                        if self.fail_open:
                            logger.warning(
                                f"FileLock timeout on {self.lock_path}; proceeding without exclusive lock"
                            )
                            break
                        with contextlib.suppress(Exception):
                            os.close(self._fd)
                        self._fd = None
                        raise FileLockTimeout(
                            f"FileLock timeout after {self.timeout}s on {self.lock_path}"
                        ) from None
                    _time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            if fcntl is not None and self._locked:
                with contextlib.suppress(Exception):
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                os.close(self._fd)
        self._fd = None
        self._locked = False
        return False


def _with_file_lock(path: Path | str, func, timeout: float = 30.0):
    """Helper to run func() while holding an exclusive lock on path.

    func receives no args and should do the read-modify-(atomic)write.
    """
    with FileLock(path, timeout=timeout):
        return func()


def _load_json_safe(path: Path, default):
    """Load JSON, tolerating missing/corrupt files.

    Fail closed: a transient read error must NOT overwrite the on-disk file
    with {}. Doing so wiped state/goals/watermarks in production (a corrupt
    read of autonomy_goals.json silently deleted every user goal). The file is
    left intact so a human can recover it; the caller runs off in-memory
    defaults for this cycle.
    """
    try:
        if not path.exists():
            return default() if callable(default) else default
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return default() if callable(default) else default
        return json.loads(raw)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning(
            f"Corrupt/unreadable {path.name}, using defaults (file left intact): {e}"
        )
        return default() if callable(default) else default


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, budget: int) -> str:
    """Truncate text to budget, marking the cut so the LLM knows it happened."""
    budget = max(0, _safe_int(budget, 0))
    if len(text) <= budget:
        return text
    suffix = "\n... [truncated]"
    if budget <= len(suffix):
        return text[:budget]
    return text[: budget - len(suffix)] + suffix


class JsonStateStore:
    """Lock-guarded JSON state + ring-buffered audit log on disk.

    autonomy.AutonomyStore and context_cleanup.ContextCleanupStore had
    byte-identical copies of all eight of these methods; the only real
    difference was the filenames and the log ring size. They drifted once
    already (one grew the fail-closed load fix months before the other), so
    the shared half lives here now. Subclasses set the file paths in __init__
    and add whatever is genuinely theirs (autonomy's goals, cleanup's control).

    Every write goes through _atomic_json_write_sync on a worker thread, and
    every read-modify-write holds `self._lock` for the whole cycle so two
    concurrent ticks can't clobber each other's state.
    """

    #: Entries kept in the audit log. Override per subclass.
    log_ring_size: int = 100

    def __init__(self, data_dir: str, *, state_file: str, log_file: str):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / state_file
        self.log_file = self.data_dir / log_file
        self._lock = asyncio.Lock()

    # -- state --

    async def load_state(self) -> dict:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            return data if isinstance(data, dict) else {}

    async def save_state(self, state: dict):
        async with self._lock:
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)

    async def patch_state(self, updates: dict) -> dict:
        """Shallow-merge `updates` into state under one lock."""
        return await self.update_state(lambda state: state.update(updates))

    async def update_state(self, fn) -> dict:
        """Read-modify-write under a single lock. fn(state) mutates in-place."""
        async with self._lock:
            state = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            if not isinstance(state, dict):
                state = {}
            fn(state)
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)
            return state

    # -- audit log --

    async def load_log(self) -> list[dict]:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.log_file, dict)
            entries = data.get("entries", []) if isinstance(data, dict) else []
            return entries if isinstance(entries, list) else []

    async def append_log_entry(self, entry: dict):
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.log_file, dict)
            entries = data.get("entries", []) if isinstance(data, dict) else []
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            entries = entries[-self.log_ring_size :]  # ring buffer
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": entries}
            )

    async def clear_log(self):
        async with self._lock:
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": []}
            )

    async def record_error(self, error: str):
        await self.patch_state({"last_error": str(error)[:2000]})
