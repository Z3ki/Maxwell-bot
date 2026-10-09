"""One-shot voice clip preparation and legacy attachment selection.

The clip is prepared and sent as ref_audio for that single line. Nothing
is stored on disk or at Mistral.
"""

from __future__ import annotations

import asyncio
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from discord_media import clean_media_url
from process_utils import communicate_process

_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
_AUDIO_EXTS = {
    ".mp3",
    ".wav",
    ".ogg",
    ".opus",
    ".flac",
    ".m4a",
    ".webm",
    ".aac",
    ".mp4",
}


def _audio_attachment(attachment: object) -> tuple[str, str] | None:
    name = str(getattr(attachment, "filename", "") or "audio")
    ctype = str(getattr(attachment, "content_type", "") or "").lower()
    ext = Path(name).suffix.lower()
    if not (ctype.startswith(("audio/", "video/")) or ext in _AUDIO_EXTS):
        return None
    url = clean_media_url(str(getattr(attachment, "url", "") or ""))
    if not _cdn_url(url):
        return None
    return name, url


def message_audio(message: object) -> list[tuple[str, str]]:
    """(filename, url) for audio on this message and a resolved reply.

    Only the requester's own clips qualify. A reply chain can reach somebody
    else's voice message, and cloning a voice the requester does not own is
    impersonation — this tool will not do it.
    """
    payloads = [message]
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None) if reference is not None else None
    if resolved is not None and _same_author(message, resolved):
        payloads.append(resolved)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for payload in payloads:
        for attachment in getattr(payload, "attachments", None) or []:
            item = _audio_attachment(attachment)
            if item is None or item[1] in seen:
                continue
            seen.add(item[1])
            found.append(item)
    return found


def _same_author(message: object, other: object) -> bool:
    mine = str(getattr(getattr(message, "author", None), "id", "") or "")
    theirs = str(getattr(getattr(other, "author", None), "id", "") or "")
    return bool(mine) and mine == theirs


def select_audio(message: object, source: object = "") -> tuple[str, str]:
    """Pick one attached clip. ``source`` is a filename or the attachment URL."""
    clips = message_audio(message)
    if not clips:
        raise ValueError("attach a short voice clip (a few seconds, one speaker)")
    wanted = str(source or "").strip()
    if wanted.lower() in {"", "attachment", "this", "audio", "clip"}:
        if len(clips) > 1:
            names = ", ".join(name for name, _url in clips)
            raise ValueError(f"several clips are attached. Name one of: {names}")
        return clips[0]
    cleaned = clean_media_url(wanted)
    for name, url in clips:
        if name.lower() == wanted.lower() or url == cleaned:
            return name, url
    names = ", ".join(name for name, _url in clips)
    raise ValueError(f"no attached clip named {wanted!r}. Attached: {names}")


def _cdn_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and host in _CDN_HOSTS
        and not parsed.username
        and parsed.port in (None, 443)
    )


class _CdnRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _cdn_url(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, "redirect blocked", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_cdn_audio(url: str) -> bytes:
    if not _cdn_url(url):
        raise ValueError("voice clips have to be attachments in this channel")
    opener = urllib.request.build_opener(_CdnRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Maxwell"})
    try:
        with opener.open(request, timeout=20) as response:
            chunks: list[bytes] = []
            while True:
                block = response.read(65536)
                if not block:
                    break
                chunks.append(block)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"could not read that clip (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise ValueError("could not read that clip") from exc
    audio = b"".join(chunks)
    if not audio:
        raise ValueError("that clip was empty")
    return audio


async def _run(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await communicate_process(process, timeout=timeout)
    except asyncio.TimeoutError:
        return 124, b"", b"timeout"
    return process.returncode or 0, stdout, stderr


async def prepare_sample(audio: bytes) -> bytes:
    """Re-encode the full clip to mono mp3; the API validates size and duration."""
    with tempfile.TemporaryDirectory(prefix="maxwell-clone-") as directory:
        folder = Path(directory)
        source = folder / "in.bin"
        target = folder / "sample.mp3"
        source.write_bytes(audio)
        code, _stdout, _stderr = await _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-protocol_whitelist", "file,pipe",
                "-format_whitelist", "wav,mp3,ogg,flac,mov,matroska,webm,aac,amr,asf,aiff,ape",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(target),
            ],
            30,
        )
        if code != 0 or not target.is_file():
            raise ValueError("could not prepare that clip for cloning")
        sample = target.read_bytes()
    if not _mp3_bytes(sample):
        raise ValueError("could not prepare that clip for cloning")
    return sample


def _mp3_bytes(sample: bytes) -> bool:
    """True for an ID3 tag or an MPEG frame sync (24 kHz is MPEG-2, not 0xFFFB)."""
    if sample.startswith(b"ID3"):
        return True
    return len(sample) >= 2 and sample[0] == 0xFF and (sample[1] & 0xE0) == 0xE0
