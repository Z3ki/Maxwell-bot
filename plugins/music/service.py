"""Guild-scoped music policy and state. Audio transport is injected and testable."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import discord

log = logging.getLogger(__name__)
READ_ACTIONS = frozenset({'status', 'queue', 'search', 'channels'})
ACTIONS = (*sorted(READ_ACTIONS), 'join', 'move', 'leave', 'play', 'skip', 'pause',
           'resume', 'stop', 'volume', 'seek', 'remove', 'clear', 'shuffle', 'repeat', 'configure')


class MusicError(ValueError):
    """Safe, user-facing error (never include transport exceptions)."""


def identifier(query: str) -> str:
    """Only YouTube identifiers reach the audio node, never arbitrary URLs."""
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise MusicError('Enter a song name or YouTube link (up to 400 characters).')
    query = query.strip()
    if any(ord(c) < 32 for c in query):
        raise MusicError('Invalid song query.')
    if '://' not in query and not query.lower().startswith(('www.', 'youtube.com/', 'youtu.be/', 'music.youtube.com/')):
        # Colon-bearing extractor prefixes and ambiguous URL-like inputs are forbidden.
        if ':' in query or re.match(r'^[\w.-]+\.[a-z]{2,}/', query, re.I):
            raise MusicError('Use a song name or an HTTPS YouTube link.')
        return 'ytsearch:' + query
    url = urlsplit(query if '://' in query else 'https://' + query)
    if (url.scheme != 'https' or url.username or url.password or url.port
            or url.hostname not in {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'}):
        raise MusicError('Only HTTPS YouTube and YouTube Music links are supported.')
    params = parse_qs(url.query)
    video = None
    if url.hostname == 'youtu.be':
        video = url.path.strip('/')
    elif url.path == '/watch':
        video = (params.get('v') or [''])[0]
    elif url.path.startswith(('/shorts/', '/live/')):
        video = url.path.split('/')[2]
    playlist = (params.get('list') or [''])[0]
    if playlist and re.fullmatch(r'[A-Za-z0-9_-]{10,100}', playlist):
        # Playlists are explicit; a watch URL with list loads that playlist.
        return 'https://www.youtube.com/playlist?list=' + playlist
    if video and re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
        return 'https://www.youtube.com/watch?v=' + video
    raise MusicError('This YouTube link is not a supported video or playlist.')


@dataclass
class Track:
    encoded: str = field(repr=False)
    title: str
    author: str
    uri: str
    duration_ms: int
    artwork: str | None = None
    seekable: bool = True
    requester: int = 0
    key: str = field(default_factory=lambda: uuid.uuid4().hex)

    def public(self):
        # Explicit allowlist; never serialize encoded tracks or node userData.
        return {'id': self.key, 'title': self.title[:180], 'artist': self.author[:120],
                'url': self.uri, 'duration_seconds': self.duration_ms // 1000,
                'artwork': self.artwork, 'requester_id': str(self.requester)}


@dataclass
class Session:
    guild: object
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    channel: object | None = None
    queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    paused: bool = False
    volume: int = 50
    repeat: str = 'off'
    last_active: float = field(default_factory=time.monotonic)
    empty_since: float | None = None
    panel: object | None = None
    text_channel: object | None = None
    error: str | None = None
    inflight: int = 0


DEFAULTS = {'default_channel': None, 'listener_controls': True, 'idle_seconds': 180,
            'queue_limit': 100, 'user_queue_limit': 20, 'dj_role': None}


class MusicService:
    def __init__(self, bot, backend, settings_path: Path, *, max_sessions=250, max_loads=8):
        self.bot, self.backend = bot, backend
        self.path = settings_path
        self.sessions: dict[int, Session] = {}
        self.settings: dict[str, dict] = {}
        self.max_sessions = max_sessions
        self.loads = asyncio.Semaphore(max_loads)
        self.settings_lock = asyncio.Lock()
        self.rates = OrderedDict()
        self._closed = False
        self.panel_update = None
        backend.service = self

    async def load_settings(self):
        def read():
            try:
                data = json.loads(self.path.read_text())
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}
        self.settings = await asyncio.to_thread(read)

    def config(self, gid):
        return {**DEFAULTS, **self.settings.get(str(gid), {})}

    async def context(self, message):
        guild = getattr(message, 'guild', None)
        user = getattr(message, 'author', None) or getattr(message, 'user', None)
        if guild is None or not user or getattr(user, 'bot', False):
            raise MusicError('Music requires a request in a server where Maxwell is installed.')
        member = guild.get_member(user.id)
        if member is None:
            raise MusicError('Your membership in this server could not be verified.')
        manager = getattr(self.bot, 'plugin_manager', None)
        if manager is not None and not manager.is_plugin_enabled_for_user('music', str(user.id), guild_id=str(guild.id)):
            raise MusicError('Music is disabled for this request. Ask a server manager about /config.')
        # Manual controls obey the same guild-wide capability switch as AI tools.
        control = getattr(self.bot, '_control', {}) or {}
        if 'plugins' in control.get('guild_disabled_capabilities', {}).get(str(guild.id), []):
            raise MusicError('Optional plugins are disabled in this server.')
        return guild, member

    def privileged(self, guild, member):
        perms = member.guild_permissions
        role = self.config(guild.id)['dj_role']
        return bool(member.id == guild.owner_id or perms.manage_guild or perms.administrator
                    or (role and any(str(r.id) == str(role) for r in member.roles)))

    def voice_channel(self, guild, member, requested=None):
        target = None
        if requested:
            value = str(requested).strip().removeprefix('<#').removesuffix('>')
            if value.isdigit():
                target = guild.get_channel(int(value))
            else:
                matches = [c for c in guild.voice_channels if c.name.casefold() == value.casefold()]
                if len(matches) > 1:
                    raise MusicError('Several voice channels have that name. Choose a channel ID.')
                target = matches[0] if matches else None
        else:
            target = getattr(getattr(member, 'voice', None), 'channel', None)
            if target is None:
                default = self.config(guild.id)['default_channel']
                target = guild.get_channel(int(default)) if default else None
        if target is None:
            raise MusicError('Which voice channel should I join? Join one or specify its name.')
        if not isinstance(target, discord.VoiceChannel) or target.guild.id != guild.id:
            raise MusicError('Choose a regular voice channel in this server (stages are not supported).')
        for subject in (member, guild.me):
            perms = target.permissions_for(subject)
            if not (perms.view_channel and perms.connect) or (subject == guild.me and not perms.speak):
                raise MusicError('You and Maxwell need access and Connect; Maxwell also needs Speak.')
        if (target.user_limit and len(target.members) >= target.user_limit
                and guild.me not in target.members and not target.permissions_for(guild.me).move_members):
            raise MusicError('That voice channel is full.')
        own = getattr(getattr(member, 'voice', None), 'channel', None)
        if getattr(own, 'id', None) != target.id and not self.privileged(guild, member):
            raise MusicError('Join that voice channel first; only a server manager or DJ can control remotely.')
        return target

    def authorize(self, session, member, action):
        if self.privileged(session.guild, member):
            return
        own = getattr(getattr(member, 'voice', None), 'channel', None)
        if session.channel and getattr(own, 'id', None) != session.channel.id:
            raise MusicError('Join the active voice channel to control its music. Session takeovers require a DJ or server manager.')
        if action in {'move', 'leave', 'stop', 'clear', 'configure'}:
            raise MusicError('This operation requires a DJ or server manager.')
        if action not in {'play', 'join', 'remove'} and not self.config(session.guild.id)['listener_controls']:
            raise MusicError('Ordinary playback controls are restricted to DJs in this server.')

    async def _load(self, query, uid, *, search=False):
        query_id = identifier(query)
        # Fail admission instead of accumulating unlimited search waiters.
        if self.loads.locked():
            raise MusicError('Music search is busy. Try again shortly.')
        async with self.loads:
            async with asyncio.timeout(20):
                tracks, playlist = await self.backend.load(query_id)
        if not tracks:
            raise MusicError('No playable tracks found. Try another title or YouTube link.')
        selected = tracks[:5] if search else (tracks[:100] if playlist else tracks[:1])
        return [Track(t.encoded, t.title, t.author, t.uri, t.duration_ms, t.artwork,
                      t.seekable, requester=uid) for t in selected]

    def snapshot(self, session):
        return {'connected': session.channel is not None,
                'voice_channel': {'id': str(session.channel.id), 'name': session.channel.name} if session.channel else None,
                'current': session.current.public() if session.current else None,
                'paused': session.paused, 'volume': session.volume, 'repeat': session.repeat,
                'position_seconds': self.backend.position(session.guild.id) // 1000 if session.current else 0,
                'queue_length': len(session.queue), 'queue': [t.public() for t in session.queue[:25]],
                'queue_note': 'Use queue with offset to view more.', 'error': session.error,
                'audio_available': self.backend.available}

    async def execute(self, message, action, *, query=None, queries=None, channel=None,
                      mode='queue', value=None, track_id=None, offset=0, settings=None):
        if self._closed:
            raise MusicError('Music is shutting down.')
        if action not in ACTIONS:
            raise MusicError('Unknown music action.')
        guild, member = await self.context(message)
        if action not in {'channels', 'status', 'queue'}:
            key, now = (guild.id, member.id), time.monotonic()
            stamps = self.rates.setdefault(key, deque(maxlen=10))
            self.rates.move_to_end(key)
            while stamps and now - stamps[0] >= 10:
                stamps.popleft()
            if len(stamps) >= 10:
                raise MusicError('Too many music requests. Wait a few seconds.')
            stamps.append(now)
            while len(self.rates) > 4096:
                self.rates.popitem(last=False)
        if action == 'channels':
            return {'channels': [{'id': str(c.id), 'name': c.name,
                                  'listeners': sum(not m.bot for m in c.members)}
                                 for c in guild.voice_channels if c.permissions_for(member).view_channel][:50],
                    'your_voice_channel': str(member.voice.channel.id) if member.voice and member.voice.channel else None}
        if action == 'search':
            tracks = await self._load(query, member.id, search=True)
            return {'results': [t.public() for t in tracks]}
        if action == 'configure':
            return await self.configure(guild, member, settings)
        session = self.sessions.get(guild.id)
        if action in {'status', 'queue'} and session is None:
            return {'connected': False, 'current': None, 'queue': [], 'audio_available': self.backend.available}
        if session is None:
            if action not in {'play', 'join'}:
                raise MusicError('There is no music session here. Ask me to join or play a song.')
            if len(self.sessions) >= self.max_sessions:
                raise MusicError('All music sessions are currently in use. Try again later.')
            # No awaits between lookup and insert; single event loop admission is atomic.
            session = Session(guild)
            self.sessions[guild.id] = session
        if session.inflight >= 8:
            raise MusicError('This music session is busy. Try again shortly.')
        session.inflight += 1
        # Bound lock wait; a stalled provider must not accumulate endless requests.
        try:
            async with asyncio.timeout(50):
                async with session.lock:
                    if self.sessions.get(guild.id) is not session:
                        raise MusicError('This session ended. Try the request again.')
                    if action in READ_ACTIONS:
                        if session.channel and not session.channel.permissions_for(member).view_channel:
                            raise MusicError('You do not have access to the active voice channel.')
                        if action == 'queue':
                            if type(offset) is not int or not 0 <= offset <= 1000:
                                raise MusicError('Queue offset must be between 0 and 1000.')
                            return {'queue': [t.public() for t in session.queue[offset:offset + 25]],
                                    'offset': offset, 'total': len(session.queue)}
                        return self.snapshot(session)
                    self.authorize(session, member, action)
                    if action in {'join', 'move', 'play'}:
                        target = self.voice_channel(guild, member, channel) if channel or session.channel is None or action == 'move' else session.channel
                        if session.channel and target.id != session.channel.id and not self.privileged(guild, member):
                            raise MusicError('Moving an active session requires a DJ or server manager.')
                    if action == 'play':
                        if mode not in {'queue', 'next', 'now'}:
                            raise MusicError('Choose queue, next, or now.')
                        if mode == 'now':
                            self.authorize(session, member, 'skip')
                        entries = queries if queries is not None else [query]
                        if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
                            raise MusicError('Request one to ten songs at a time.')
                        own_count = sum(t.requester == member.id for t in session.queue)
                        config = self.config(guild.id)
                        tracks = []
                        for item in entries:
                            tracks.extend(await self._load(item, member.id))
                        if len(session.queue) + len(tracks) > config['queue_limit']:
                            raise MusicError('The queue is full. Remove songs before adding this request.')
                        if own_count + len(tracks) > config['user_queue_limit'] and not self.privileged(guild, member):
                            raise MusicError('You have reached your queued-song allowance.')
                        # Nothing is queued or moved until all loading and policy checks succeed.
                        await self._join(session, target)
                        session.text_channel = getattr(message, 'channel', None) or session.text_channel
                        if session.current is None or mode == 'now':
                            await self._start(session, tracks[0])
                            session.queue[0:0] = tracks[1:]
                            result = {'started': session.current.public(), 'added': len(tracks) - 1}
                        else:
                            if mode == 'next':
                                session.queue[0:0] = tracks
                            else:
                                session.queue.extend(tracks)
                            result = {'queued': [t.public() for t in tracks], 'added': len(tracks)}
                    elif action in {'join', 'move'}:
                        await self._join(session, target)
                        result = {'joined': {'id': str(target.id), 'name': target.name}}
                    elif action == 'leave':
                        await self._leave(session)
                        return {'left': True}
                    elif action in {'pause', 'resume'}:
                        if session.current is None:
                            raise MusicError('Nothing is playing.')
                        await self.backend.pause(guild.id, action == 'pause')
                        session.paused = action == 'pause'
                        result = {'paused': session.paused}
                    elif action == 'skip':
                        await self._advance(session, manual=True)
                        result = {'skipped': True, 'current': session.current.public() if session.current else None}
                    elif action == 'stop':
                        await self.backend.stop(guild.id)
                        session.current, session.paused = None, False
                        session.queue.clear()
                        result = {'stopped': True, 'queue_cleared': True}
                    elif action == 'volume':
                        if type(value) is not int or not 0 <= value <= 100:
                            raise MusicError('Volume must be an integer from 0 to 100.')
                        await self.backend.volume(guild.id, value)
                        session.volume = value
                        result = {'volume': value}
                    elif action == 'seek':
                        if (not session.current or not session.current.seekable or type(value) is not int
                                or not 0 <= value * 1000 < session.current.duration_ms):
                            raise MusicError('Seek requires a seekable song and seconds within its duration.')
                        await self.backend.seek(guild.id, value * 1000)
                        result = {'position_seconds': value}
                    elif action == 'remove':
                        track = next((t for t in session.queue if t.key == track_id), None)
                        if track is None:
                            raise MusicError('That queued track no longer exists. Read the queue again.')
                        if track.requester != member.id and not self.privileged(guild, member):
                            raise MusicError('You can only remove your own songs.')
                        session.queue.remove(track)
                        result = {'removed': track.public()}
                    elif action == 'clear':
                        session.queue.clear()
                        result = {'queue_cleared': True}
                    elif action == 'shuffle':
                        random.SystemRandom().shuffle(session.queue)
                        result = {'shuffled': True}
                    elif action == 'repeat':
                        if value not in {'off', 'track', 'queue'}:
                            raise MusicError('Repeat must be off, track, or queue.')
                        session.repeat = value
                        result = {'repeat': value}
                    session.last_active = time.monotonic()
                    session.error = None
                    await self.publish(session)
                    return {**result, 'state': self.snapshot(session)}
        finally:
            session.inflight -= 1
            # A failed first request leaves neither a reserved slot nor a lock behind.
            if session.channel is None and not session.current and not session.queue:
                if self.sessions.get(guild.id) is session and not session.lock.locked() and not session.inflight:
                    self.sessions.pop(guild.id, None)

    async def _join(self, session, target):
        if session.channel is not None and session.channel.id == target.id:
            return
        await self.backend.join(session.guild, target)
        session.channel = target
        session.empty_since = None

    async def _start(self, session, track, *, position=0):
        track = replace(track, key=uuid.uuid4().hex)
        try:
            await self.backend.play(session.guild.id, track, volume=session.volume, position=position)
        except BaseException:
            session.current, session.paused = None, False
            session.last_active = time.monotonic()
            session.error = 'Playback could not be confirmed. The queue has been preserved.'
            await self.publish(session)
            raise
        session.current, session.paused, session.error = track, False, None

    async def _advance(self, session, *, manual=False, failed=False):
        old = session.current
        if old and not manual and not failed and session.repeat == 'track':
            await self._start(session, old)
            return
        # Append looped tracks only after a successful transition.
        if session.queue:
            next_track = session.queue[0]
            await self._start(session, next_track)
            session.queue.pop(0)
            if old and not failed and session.repeat == 'queue':
                session.queue.append(old)
        elif old and not manual and not failed and session.repeat == 'queue':
            await self._start(session, old)
        else:
            await self.backend.stop(session.guild.id)
            session.current, session.paused = None, False
            session.last_active = time.monotonic()

    async def ended(self, gid, key, *, failed=False):
        session = self.sessions.get(gid)
        if not session:
            return
        async with session.lock:
            if self.sessions.get(gid) is not session or not session.current or session.current.key != key:
                return  # Stale end/replaced events cannot advance a new track.
            if failed:
                session.error = 'The audio source could not play that song; trying the next track.'
            try:
                await self._advance(session, failed=failed)
            except Exception as exc:
                log.warning('Music transition failed guild=%s type=%s', gid, type(exc).__name__)
                # Preserve the queue for reconnect or a later explicit skip.
                session.current = None
                session.error = 'Audio is unavailable; the queue has been preserved. Try skip after recovery.'
                session.last_active = time.monotonic()
            await self.publish(session)

    async def disconnected(self, gid):
        session = self.sessions.get(gid)
        if session:
            async with session.lock:
                await self.backend.destroy(gid)
                session.current = None
                session.queue.clear()
                session.channel = None
                self.sessions.pop(gid, None)
                await self.publish(session)

    async def tick(self):
        now = time.monotonic()
        for session in list(self.sessions.values()):
            if session.lock.locked():
                continue
            async with session.lock:
                if self.sessions.get(session.guild.id) is not session:
                    continue
                humans = bool(session.channel and any(not m.bot for m in session.channel.members))
                if humans:
                    session.empty_since = None
                elif session.empty_since is None:
                    session.empty_since = now
                timeout = self.config(session.guild.id)['idle_seconds']
                if ((not session.current or session.paused) and now - session.last_active >= timeout
                        or session.empty_since is not None and now - session.empty_since >= timeout):
                    try:
                        await self._leave(session)
                    except Exception as exc:
                        log.warning('Music idle cleanup failed guild=%s type=%s', session.guild.id, type(exc).__name__)

    async def _leave(self, session):
        await self.backend.leave(session.guild)
        session.channel, session.current = None, None
        session.queue.clear()
        self.sessions.pop(session.guild.id, None)
        await self.publish(session)

    async def configure(self, guild, member, changes):
        if not (member.guild_permissions.manage_guild or member.guild_permissions.administrator or member.id == guild.owner_id):
            raise MusicError('Only server managers can configure music.')
        if changes is None:
            return {'settings': self.config(guild.id)}
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
            raise MusicError('Unknown music setting.')
        for key, value in changes.items():
            if key in {'queue_limit', 'user_queue_limit', 'idle_seconds'}:
                bounds = {'queue_limit': (1, 100), 'user_queue_limit': (1, 100), 'idle_seconds': (30, 3600)}[key]
                if type(value) is not int or not bounds[0] <= value <= bounds[1]:
                    raise MusicError(f'{key} must be between {bounds[0]} and {bounds[1]}.')
            elif key == 'listener_controls' and type(value) is not bool:
                raise MusicError('listener_controls must be true or false.')
            elif key == 'default_channel' and value is not None:
                if not str(value).isdigit() or not isinstance(guild.get_channel(int(value)), discord.VoiceChannel):
                    raise MusicError('Default channel must be a voice channel in this server.')
            elif key == 'dj_role' and value is not None:
                if not str(value).isdigit() or guild.get_role(int(value)) is None:
                    raise MusicError('DJ role must be a role in this server.')
        async with self.settings_lock:
            updated = {**self.settings, str(guild.id): {**self.config(guild.id), **changes}}
            def write():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix('.tmp')
                temp.write_text(json.dumps(updated, indent=2))
                temp.replace(self.path)
            await asyncio.to_thread(write)
            self.settings = updated
        return {'saved': True, 'settings': self.config(guild.id)}

    async def publish(self, session):
        if self.panel_update:
            try:
                async with asyncio.timeout(3):
                    await self.panel_update(session)
            except Exception as exc:
                log.warning('Music panel update failed guild=%s type=%s', session.guild.id, type(exc).__name__)

    async def close(self):
        self._closed = True
        async def leave(session):
            try:
                async with asyncio.timeout(5):
                    async with session.lock:
                        await self._leave(session)
            except Exception as exc:
                log.warning('Music shutdown failed guild=%s type=%s', session.guild.id, type(exc).__name__)
        try:
            # Per-guild timeouts run concurrently inside the plugin's 15s teardown budget.
            await asyncio.gather(*(leave(s) for s in list(self.sessions.values())))
        finally:
            self.sessions.clear()
            await self.backend.close()
