"""Bounded, paced Last.fm requests; provider messages never become diagnostics."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

import aiohttp

API_URL = 'https://ws.audioscrobbler.com/2.0/'
_CREDENTIAL = re.compile(r'^[0-9a-fA-F]{32}$')


class LastFmError(Exception):
    def __init__(self, message: str, *, code: int = 0, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def valid_api_credentials(key, secret):
    return bool(_CREDENTIAL.fullmatch(key or '') and _CREDENTIAL.fullmatch(secret or ''))

def _valid_opaque_token(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024 and value.isascii()
            and all(32 < ord(char) < 127 for char in value))



def signature(parameters: dict, secret: str) -> str:
    payload = ''.join(f'{key}{parameters[key]}' for key in sorted(parameters)
                      if key not in {'api_sig', 'format', 'callback'}) + secret
    # Last.fm's api_sig scheme mandates MD5; the hash choice is not ours.
    return hashlib.md5(payload.encode('utf-8'), usedforsecurity=False).hexdigest()


# 8 is Last.fm's "operation failed, try again". 11/16 are service outages.
# 29 is rate limiting. 10/13/26 are operator credentials: hold the play.
RETRYABLE_PROVIDER_CODES = frozenset({8, 11, 16, 29})
HELD_PROVIDER_CODES = frozenset({8, 10, 13, 26})


def provider_error(code: int, *, http_status: int = 0) -> LastFmError:
    messages = {
        4: 'Last.fm authorization is invalid. Start /lastfm connect again.',
        6: 'Last.fm rejected the track or request parameters.',
        8: 'Last.fm could not complete the request. Try again later.',
        9: 'Your Last.fm session was revoked. Reconnect with /lastfm connect.',
        10: 'Last.fm rejected the operator API key. Ask the operator to check LASTFM_API_KEY.',
        11: 'Last.fm is temporarily unavailable. Try again later.',
        13: 'Last.fm rejected the API signature. Ask the operator to check LASTFM_API_SECRET.',
        14: 'Approval is not complete. Approve Maxwell in your Last.fm browser window, then finish linking.',
        15: 'This Last.fm approval has expired. Start /lastfm connect again.',
        16: 'Last.fm is temporarily unavailable. Try again later.',
        26: 'Last.fm suspended the operator API key. Ask the operator to check the application.',
        29: 'Last.fm is rate limiting requests. Try again later.',
    }
    retryable = (
        code in RETRYABLE_PROVIDER_CODES
        or code in HELD_PROVIDER_CODES
        or http_status >= 500
    )
    return LastFmError(messages.get(code, 'Last.fm rejected this request.'), code=code,
                       retryable=retryable)


class LastFmClient:
    def __init__(self, api_key: str, api_secret: str, *, session=None,
                 requests_per_second: float = 2, concurrency: int = 2):
        if requests_per_second <= 0 or concurrency < 1:
            raise ValueError('Last.fm pacing and concurrency must be positive')
        self.api_key = api_key
        self._secret = api_secret
        self._session = session
        self._own_session = session is None
        self._interval = 1 / requests_per_second
        self._slots = asyncio.Semaphore(concurrency)
        self._pace_lock = asyncio.Lock()
        self._next_start = 0.0
        self._closed = False

    async def _request(self, method: str, parameters: dict, *, write=False) -> dict:
        if self._closed:
            raise LastFmError('Last.fm is shutting down. Try again later.')
        if not valid_api_credentials(self.api_key, self._secret):
            raise LastFmError('Last.fm requires operator LASTFM_API_KEY and LASTFM_API_SECRET configuration.')
        values = {'method': method, 'api_key': self.api_key, **parameters}
        values['api_sig'] = signature(values, self._secret)
        values['format'] = 'json'
        async with self._slots:
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15, connect=5),
                    connector=aiohttp.TCPConnector(limit=2), trust_env=False)
            async with self._pace_lock:
                await asyncio.sleep(max(0, self._next_start - time.monotonic()))
                self._next_start = time.monotonic() + self._interval
            try:
                async with self._session.request('POST' if write else 'GET', API_URL,
                        **({'data': values} if write else {'params': values}),
                        allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15, connect=5)) as response:
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise LastFmError('Last.fm returned an invalid response.')
                    try:
                        result = json.loads(body)
                    except (ValueError, UnicodeError):
                        result = None
                    if isinstance(result, dict) and 'error' in result:
                        try:
                            code = int(result['error'])
                        except (TypeError, ValueError):
                            raise LastFmError('Last.fm returned an invalid error response.') from None
                        raise provider_error(code, http_status=response.status)
                    if response.status != 200:
                        raise LastFmError('Last.fm could not complete the request. Try again later.',
                                          code=29 if response.status == 429 else 0,
                                          retryable=response.status == 429 or response.status >= 500)
                    if not isinstance(result, dict):
                        raise LastFmError('Last.fm returned an invalid response.')
                    return result
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise LastFmError('Last.fm could not be reached. Try again later.', retryable=True) from None

    async def get_token(self) -> str:
        result = await self._request('auth.getToken', {})
        token = result.get('token')
        if not _valid_opaque_token(token):
            raise LastFmError('Last.fm returned an invalid authorization response.')
        return token

    async def get_session(self, token: str) -> tuple[str, str]:
        result = await self._request('auth.getSession', {'token': token})
        session = result.get('session')
        if not isinstance(session, dict):
            raise LastFmError('Last.fm returned an invalid session response.')
        name, key = session.get('name'), session.get('key')
        if not isinstance(name, str) or not name or len(name) > 255 or not _valid_opaque_token(key):
            raise LastFmError('Last.fm returned an invalid session response.')
        return name, key

    @staticmethod
    def _track(artist, track, duration):
        if (not isinstance(artist, str) or not artist.strip() or len(artist) > 1000
                or not isinstance(track, str) or not track.strip() or len(track) > 1000
                or isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0):
            raise LastFmError('A known artist, title and duration are required.', code=6)
        return {'artist': artist, 'track': track, 'duration': str(duration)}

    async def update_now_playing(self, session_key: str, *, artist: str, track: str, duration: int) -> dict:
        result = await self._request('track.updateNowPlaying',
            {'sk': session_key, **self._track(artist, track, duration)}, write=True)
        notice = result.get('nowplaying')
        if not isinstance(notice, dict) or not isinstance(notice.get('ignoredMessage'), dict):
            raise LastFmError('Last.fm returned an invalid now-playing response.')
        return result

    async def scrobble(self, session_key: str, *, artist: str, track: str, duration: int,
                       timestamp: int, chosen_by_user: bool | None = None) -> dict:
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
            raise LastFmError('A valid playback start time is required.', code=6)
        values = {'sk': session_key, 'timestamp': str(timestamp), **self._track(artist, track, duration)}
        if chosen_by_user is not None:
            values['chosenByUser'] = '1' if chosen_by_user else '0'
        result = await self._request('track.scrobble', values, write=True)
        try:
            scrobbles = result['scrobbles']
            accepted, ignored = (int(scrobbles['@attr'][key]) for key in ('accepted', 'ignored'))
            entry = scrobbles['scrobble']
            if isinstance(entry, list) and len(entry) == 1:
                entry = entry[0]
            ignored_code = int(entry['ignoredMessage']['code'])
            if accepted not in (0, 1) or ignored not in (0, 1) or accepted + ignored != 1:
                raise ValueError
            if (accepted == 1) != (ignored_code == 0):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise LastFmError('Last.fm returned an invalid scrobble acknowledgement.') from None
        return result

    async def close(self):
        self._closed = True
        if self._own_session and self._session is not None:
            await self._session.close()
