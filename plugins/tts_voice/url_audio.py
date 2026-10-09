"""Public reference-audio downloads with DNS and redirect SSRF checks."""
from urllib.parse import urljoin, urlsplit

import aiohttp

from discord_media import clean_media_url
from tooling.helpers import _get_shared_session, _is_safe_url



def validate_reference_url(url):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError('reference audio needs a public HTTP or HTTPS URL')
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or not _is_safe_url(url):
            raise ValueError('reference audio needs a public HTTP or HTTPS URL without credentials')
        _ = parsed.port  # Reject malformed ports before requesting.
    except ValueError:
        raise ValueError('reference audio needs a public HTTP or HTTPS URL without credentials') from None
    return url


async def download_reference_audio(url):
    current = validate_reference_url(clean_media_url(url.strip()))
    session = await _get_shared_session()  # SafeResolver checks actual connect-time DNS.
    try:
        # Total bounded separately from each HTTP hop.
        import asyncio
        async with asyncio.timeout(20):
            for _ in range(6):
                validate_reference_url(current)
                async with session.get(current, allow_redirects=False,
                                       timeout=aiohttp.ClientTimeout(total=20),
                                       headers={'User-Agent': 'Maxwell', 'Accept': 'audio/*,video/*,application/octet-stream'}) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('reference audio redirect has no destination')
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise ValueError(f'could not read reference audio (HTTP {response.status})')
                    mime = response.headers.get('Content-Type', '').split(';', 1)[0].lower()
                    if mime and not mime.startswith(('audio/', 'video/')) and mime not in {'application/octet-stream', 'binary/octet-stream', 'application/ogg'}:
                        raise ValueError('that URL does not return a direct audio/video file; use a media download link')
                    raw = await response.read()
                    if not raw:
                        raise ValueError('reference audio was empty')
                    return raw
            raise ValueError('reference audio has too many redirects')
    except ValueError:
        raise
    except Exception:
        raise ValueError('could not download reference audio; use a reachable public media link') from None
