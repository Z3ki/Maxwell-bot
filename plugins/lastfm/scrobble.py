"""Opt-in listening accounting and a credential-free, durable Last.fm outbox."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import stat
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

from .client import LastFmError

log = logging.getLogger(__name__)
_BATCH = 64


@dataclass(frozen=True)
class _Job:
    job_id: str
    user_id: str
    link_id: str
    guild_id: int
    instance_id: str
    artist: str
    track: str
    duration: int
    timestamp: int
    chosen_by_user: bool | None
    attempts: int = 0
    next_attempt: float = 0


@dataclass
class _Listener:
    link_id: str
    timestamp: int = 0  # Filled when first unpaused listening begins, before any job.
    active: bool = False
    listened: float = 0
    notified: bool = False


@dataclass
class _Play:
    session: object
    source_key: str
    instance_id: str
    artist: str
    track: str
    duration: int
    threshold: float
    last: float
    paused: bool
    requester: str | None
    autoplay: bool
    listeners: dict[str, _Listener] = field(default_factory=dict)
    qualified: set[str] = field(default_factory=set)


class _Outbox:
    """All methods, including open/close, run on one dedicated disk thread."""

    def __init__(self, path):
        self.path = Path(path)
        self.connection = None

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise ValueError('Last.fm storage must be a private directory.')
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Last.fm storage must be a regular file.')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=DELETE')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            link_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            instance_id TEXT NOT NULL,
            artist TEXT NOT NULL,
            track TEXT NOT NULL,
            duration INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            chosen_by_user INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0
        )''')
        self.connection.execute('CREATE INDEX IF NOT EXISTS outbox_order ON outbox(timestamp, sequence)')
        self.connection.execute('''CREATE INDEX IF NOT EXISTS outbox_owner_order
            ON outbox(user_id, timestamp, sequence)''')
        self.connection.commit()
        return dict(self.connection.execute('SELECT user_id, COUNT(*) FROM outbox GROUP BY user_id'))

    def add(self, jobs):
        inserted = []
        with self.connection:
            for job in jobs:
                cursor = self.connection.execute('''INSERT OR IGNORE INTO outbox
                    (job_id,user_id,link_id,guild_id,instance_id,artist,track,duration,
                     timestamp,chosen_by_user,attempts,next_attempt)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (job.job_id, job.user_id, job.link_id, job.guild_id, job.instance_id,
                     job.artist, job.track, job.duration, job.timestamp,
                     job.chosen_by_user, job.attempts, job.next_attempt))
                if cursor.rowcount:
                    inserted.append(job.user_id)
        return inserted

    def oldest(self, now):
        # One head per owner prevents a paused/retrying account's backlog from
        # hiding another account behind the bounded result window.
        head = '''NOT EXISTS (SELECT 1 FROM outbox older
            WHERE older.user_id=o.user_id AND
            (older.timestamp<o.timestamp OR
             (older.timestamp=o.timestamp AND older.sequence<o.sequence)))'''
        rows = self.connection.execute(f'''SELECT o.* FROM outbox o
            WHERE {head} AND o.next_attempt<=?
            ORDER BY o.timestamp,o.sequence LIMIT ?''', (now, _BATCH)).fetchall()
        next_at = self.connection.execute(f'''SELECT MIN(o.next_attempt) FROM outbox o
            WHERE {head} AND o.next_attempt>?''', (now,)).fetchone()[0]
        jobs = []
        for row in rows:
            values = {key: row[key] for key in _Job.__dataclass_fields__}
            if values['chosen_by_user'] is not None:
                values['chosen_by_user'] = bool(values['chosen_by_user'])
            jobs.append(_Job(**values))
        return jobs, max(0, next_at - now) if next_at is not None else None

    def delete(self, job_id):
        with self.connection:
            row = self.connection.execute('SELECT user_id FROM outbox WHERE job_id=?', (job_id,)).fetchone()
            self.connection.execute('DELETE FROM outbox WHERE job_id=?', (job_id,))
        return row['user_id'] if row else None

    def retry(self, job, when):
        with self.connection:
            self.connection.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=? WHERE job_id=?',
                                    (when, job.job_id))

    def forget(self, user_id):
        with self.connection:
            self.connection.execute('DELETE FROM outbox WHERE user_id=?', (user_id,))

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class ScrobbleManager:
    """One supervisor, one HTTP flight, bounded disk batches; music only touches memory.

    Qualified plays wait in memory only until committed, not until a provider
    responds. The supervisor continues committing while its single HTTP flight
    is stalled. There is deliberately no overflow policy that loses scrobbles.
    Now-playing notices, unlike qualified plays, are coalesced and disposable.
    """

    def __init__(self, music, accounts, store_path):
        self.music, self.accounts = music, accounts
        self._store = _Outbox(store_path)
        self._executor = None
        self._worker = None
        self._flight = None
        self._flight_job = None
        self._flight_kind = None
        self._wake = asyncio.Event()
        self._plays: dict[int, _Play] = {}
        self._pending: OrderedDict[str, _Job] = OrderedDict()
        self._now_playing: OrderedDict[str, _Job] = OrderedDict()
        self._counts: dict[str, int] = {}
        self._forgotten_links: dict[str, set[str]] = {}
        self._forget_requests = deque()
        self._started = False
        self._closing = False
        self._closed = False
        self._observer = self._observe
        self._monotonic = time.monotonic
        self._clock = time.time

    async def start(self):
        if self._started or self._closed:
            return
        self._started = True
        if not self.accounts.configured:
            return
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lastfm-outbox')
        try:
            self._counts = await self._disk(self._store.open)
        except Exception:
            await self._disk(self._store.close)
            self._executor.shutdown(wait=False)
            self._executor = None
            raise RuntimeError('Last.fm listening storage could not be opened.') from None
        self.music.playback_observer = self._observer
        self._worker = asyncio.create_task(self._run(), name='lastfm-scrobble-worker')
        self._wake.set()

    async def _disk(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    def _account(self, user_id):
        account = self.accounts.account_for(user_id)
        if account is None or str(account.user_id) != str(user_id):
            self._forgotten_links.pop(str(user_id), None)
            return None
        forgotten = self._forgotten_links.get(str(user_id))
        if forgotten and account.link_id in forgotten:
            return None
        if forgotten:
            self._forgotten_links.pop(str(user_id), None)
        return account

    @staticmethod
    def _channel_id(channel):
        return getattr(channel, 'id', None)

    def _eligible(self, session, member, voice=None, channel=None):
        if getattr(member, 'bot', False):
            return None
        channel = session.channel if channel is None else channel
        if channel is None:
            return None
        voice = getattr(member, 'voice', None) if voice is None else voice
        if (voice is None or self._channel_id(getattr(voice, 'channel', None)) != self._channel_id(channel)
                or getattr(voice, 'deaf', False) or getattr(voice, 'self_deaf', False)):
            return None
        return self._account(str(member.id))

    def _channel(self, session):
        # The cached bot voice state also handles moderator moves/disconnects
        # before the music transport has updated its session.channel.
        own = getattr(getattr(session.guild, 'me', None), 'voice', None)
        return getattr(own, 'channel', None)

    def _job(self, play, user_id, link_id):
        chosen = False if play.autoplay else (
            user_id == play.requester if play.requester is not None else None)
        return _Job(f'{play.instance_id}:{user_id}', user_id, link_id,
                    int(play.session.guild.id), play.instance_id, play.artist,
                    play.track, play.duration, play.listeners[user_id].timestamp, chosen)

    def _charge(self, play, now, eligible_ids=None):
        elapsed = max(0, now - play.last)
        play.last = now
        if play.paused:
            return
        for user_id, listener in play.listeners.items():
            account = self._account(user_id)
            if (not listener.active or account is None or account.link_id != listener.link_id
                    or eligible_ids is not None and user_id not in eligible_ids):
                continue
            listener.listened += elapsed
            if listener.listened >= play.threshold and user_id not in play.qualified:
                job = self._job(play, user_id, listener.link_id)
                self._pending[job.job_id] = job
                play.qualified.add(user_id)
                self._wake.set()

    def _sync(self, play, *, override=None, channel=None, disconnected=False):
        for listener in play.listeners.values():
            listener.active = False
        channel = self._channel(play.session) if channel is None else channel
        if disconnected or channel is None:
            return
        members = {str(member.id): member for member in getattr(channel, 'members', ())}
        if override:
            members[str(override[0].id)] = override[0]
        for user_id, member in members.items():
            voice = override[1] if override and str(override[0].id) == user_id else None
            account = self._eligible(play.session, member, voice, channel)
            if account is None:
                continue
            listener = play.listeners.get(user_id)
            if listener is None or listener.link_id != account.link_id:
                listener = play.listeners[user_id] = _Listener(account.link_id)
            listener.active = True
            if not play.paused and not listener.notified:
                listener.timestamp = int(self._clock())
                listener.notified = True
                self._now_playing[user_id] = self._job(play, user_id, account.link_id)
                self._now_playing.move_to_end(user_id)
                self._wake.set()

    def _remove_notices(self, instance_id):
        for user_id, job in list(self._now_playing.items()):
            if job.instance_id == instance_id:
                self._now_playing.pop(user_id, None)

    def _observe(self, session, event):
        if self._closed or self._closing or self._worker is None:
            return
        gid = int(session.guild.id)
        now = self._monotonic()
        play = self._plays.get(gid)
        if play is not None:
            # Periodic reconciliation is conservative if a gateway transition
            # was missed: an absent/deaf listener gets no uncertain interval.
            eligible_ids = None
            if event == 'tick':
                channel = self._channel(session)
                eligible_ids = {str(member.id) for member in getattr(channel, 'members', ())
                                if self._eligible(session, member, channel=channel) is not None}
            self._charge(play, now, eligible_ids)
        if event in {'start', 'end'}:
            if play is not None:
                self._remove_notices(play.instance_id)
                self._plays.pop(gid, None)
            if event == 'end':
                return
            track = session.current
            if track is None:
                return
            duration_ms = getattr(track, 'duration_ms', 0)
            artist, title = getattr(track, 'author', ''), getattr(track, 'title', '')
            if (not isinstance(duration_ms, int) or duration_ms <= 30000
                    or not getattr(track, 'seekable', True) or getattr(track, 'is_stream', False)
                    or not isinstance(artist, str) or not artist.strip()
                    or not isinstance(title, str) or not title.strip()):
                return
            requester = getattr(track, 'requester', None)
            play = _Play(session, track.key, uuid.uuid4().hex, artist.strip(), title.strip(),
                         (duration_ms + 999) // 1000, min(duration_ms / 2000, 240),
                         now, bool(session.paused),
                         str(requester) if requester else None, bool(getattr(track, 'autoplay', False)))
            self._plays[gid] = play
        elif play is not None:
            if session.current is None or session.current.key != play.source_key:
                self._remove_notices(play.instance_id)
                self._plays.pop(gid, None)
                return
            play.paused = bool(session.paused)
        if play is not None:
            self._sync(play)
            if play.paused:
                self._remove_notices(play.instance_id)

    async def tick(self):
        if self._worker is None or self._closing or self._closed:
            return
        for play in list(self._plays.values()):
            if self.music.sessions.get(play.session.guild.id) is play.session:
                self._observe(play.session, 'tick')
            else:
                self._remove_notices(play.instance_id)
                self._plays.pop(int(play.session.guild.id), None)
        self._wake.set()

    async def voice_state_changed(self, member, before, after):
        if self._worker is None or self._closing or self._closed:
            return
        gid = int(member.guild.id)
        play = self._plays.get(gid)
        if play is None:
            return
        self._charge(play, self._monotonic())
        bot_id = getattr(getattr(self.music.bot, 'user', None), 'id', None)
        if member.id == bot_id or member.id == getattr(getattr(member.guild, 'me', None), 'id', None):
            self._sync(play, channel=getattr(after, 'channel', None),
                       disconnected=getattr(after, 'channel', None) is None)
        else:
            self._sync(play, override=(member, after))
        self._wake.set()

    async def pending_count(self, user_id):
        user_id = str(user_id)
        return self._counts.get(user_id, 0) + sum(job.user_id == user_id for job in self._pending.values())

    async def forget_user(self, user_id):
        user_id = str(user_id)
        links = self._forgotten_links.setdefault(user_id, set())
        for play in self._plays.values():
            listener = play.listeners.pop(user_id, None)
            if listener is not None:
                links.add(listener.link_id)
        for job in list(self._pending.values()):
            if job.user_id == user_id:
                links.add(job.link_id)
                self._pending.pop(job.job_id, None)
        notice = self._now_playing.pop(user_id, None)
        if notice:
            links.add(notice.link_id)
        if self._flight_job is not None and self._flight_job.user_id == user_id:
            links.add(self._flight_job.link_id)
            if self._flight is not asyncio.current_task():
                self._flight.cancel()
        if self._worker is None or self._closed:
            return
        future = asyncio.get_running_loop().create_future()
        self._forget_requests.append((user_id, future))
        self._wake.set()
        await future
        # Retain only a still-current revoked consent, not historical owners.
        account = self.accounts.account_for(user_id)
        links.intersection_update((account.link_id,) if account is not None else ())
        if not links:
            self._forgotten_links.pop(user_id, None)

    @staticmethod
    def _response_kind(response):
        # Never infer acceptance from HTTP 200, returned corrections, or merely
        # the presence of a scrobble object. Single submissions require one ack.
        try:
            scrobbles = response['scrobbles']
            accepted = int(scrobbles['@attr']['accepted'])
            ignored = int(scrobbles['@attr']['ignored'])
            result = scrobbles['scrobble']
            if isinstance(result, list):
                if len(result) != 1:
                    return 'rejected'
                result = result[0]
            code = int(result['ignoredMessage']['code'])
        except (KeyError, TypeError, ValueError, OverflowError):
            return 'rejected'
        if accepted == 1 and ignored == 0 and code == 0:
            return 'accepted'
        if accepted == 0 and ignored == 1 and code in {4, 5}:
            return 'later'  # clock skew / daily quota, not permanent metadata rejection
        return 'rejected'

    def _live_notice(self, job):
        play = self._plays.get(job.guild_id)
        if play is None or play.instance_id != job.instance_id or play.paused:
            return False
        listener = play.listeners.get(job.user_id)
        return bool(listener and listener.active and listener.link_id == job.link_id)

    def _music_busy(self, job):
        session = self.music.sessions.get(job.guild_id)
        return bool(session and (session.lock.locked() or session.play_lock.locked()))

    async def _deliver(self, job, kind):
        account = self._account(job.user_id)
        if account is None or account.link_id != job.link_id:
            return 'identity'
        try:
            if kind == 'now':
                await self.accounts.client.update_now_playing(account.session_key, artist=job.artist,
                                                            track=job.track, duration=job.duration)
                return 'notice'
            response = await self.accounts.client.scrobble(
                account.session_key, artist=job.artist, track=job.track,
                duration=job.duration, timestamp=job.timestamp,
                chosen_by_user=job.chosen_by_user)
            return self._response_kind(response)
        except LastFmError as exc:
            if exc.code == 9:
                self._forgotten_links.setdefault(job.user_id, set()).add(job.link_id)
                await self.accounts.invalidate(job.user_id, job.link_id)
                return 'identity'
            return 'retry' if exc.retryable else 'rejected'
        except Exception as exc:
            # Transport/client surprises must not lose a qualified play, and
            # exception text may contain secrets or a signed request.
            log.warning('Last.fm delivery failed type=%s', type(exc).__name__)
            return 'retry'

    async def _delete(self, job):
        user_id = await self._disk(self._store.delete, job.job_id)
        if user_id is not None:
            remaining = self._counts.get(user_id, 0) - 1
            if remaining > 0:
                self._counts[user_id] = remaining
            else:
                self._counts.pop(user_id, None)

    async def _complete(self):
        if self._flight is None or not self._flight.done():
            return
        job, kind = self._flight_job, self._flight_kind
        if not self._flight.cancelled():
            try:
                result = self._flight.result()
            except Exception as exc:
                log.warning('Last.fm delivery task failed type=%s', type(exc).__name__)
                result = 'retry'
            if kind == 'scrobble':
                if result in {'accepted', 'rejected', 'identity'}:
                    await self._delete(job)
                else:
                    delay = 86400 if result == 'later' else min(3600, 5 * (2 ** min(job.attempts, 10)))
                    await self._disk(self._store.retry, job, self._clock() + delay)
        self._flight = self._flight_job = self._flight_kind = None

    async def _cycle(self):
        # Unlink deletion follows any already accepted insert on the serial disk
        # executor. Newly queued work is fenced synchronously by forget_user.
        while self._forget_requests:
            user_id, future = self._forget_requests[0]
            await self._disk(self._store.forget, user_id)
            self._counts.pop(user_id, None)
            self._forget_requests.popleft()
            if not future.done():
                future.set_result(None)
        if self._pending:
            jobs = tuple(islice(self._pending.values(), _BATCH))
            inserted = await self._disk(self._store.add, jobs)
            for user_id in inserted:
                self._counts[user_id] = self._counts.get(user_id, 0) + 1
            for job in jobs:
                self._pending.pop(job.job_id, None)
        await self._complete()
        if self._closing or self._flight is not None:
            return None
        delay = None
        if any(self._counts.values()):
            now = self._clock()
            jobs, delay = await self._disk(self._store.oldest, now)
            blocked = set()
            for job in jobs:
                if job.user_id in blocked:
                    continue
                account = self._account(job.user_id)
                if account is not None and account.link_id != job.link_id:
                    # A new consent generation permanently rejects the old
                    # identity; it must never borrow the new account's key.
                    await self._delete(job)
                    self._wake.set()
                    continue
                if account is None:
                    await self._disk(self._store.retry, job, now + 60)
                    delay = 60 if delay is None else min(delay, 60)
                    blocked.add(job.user_id)
                    continue
                if self._music_busy(job):
                    delay = .1 if delay is None else min(delay, .1)
                    blocked.add(job.user_id)
                    continue
                self._launch(job, 'scrobble')
                return delay
        # Stale notices are discarded, and there is never more than one
        # outstanding notice per user, irrespective of repeat/seek activity.
        for user_id, job in list(self._now_playing.items()):
            account = self._account(user_id)
            if not self._live_notice(job) or account is None or account.link_id != job.link_id:
                self._now_playing.pop(user_id, None)
                continue
            if self._music_busy(job):
                delay = .1 if delay is None else min(delay, .1)
                continue
            self._now_playing.pop(user_id, None)
            self._launch(job, 'now')
            break
        return delay

    def _launch(self, job, kind):
        if self._closing:
            return
        self._flight_job, self._flight_kind = job, kind
        self._flight = asyncio.create_task(self._deliver(job, kind), name='lastfm-delivery')

    async def _run(self):
        while True:
            self._wake.clear()
            failed = False
            try:
                delay = await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Last.fm outbox worker failed type=%s', type(exc).__name__)
                if self._closing:
                    raise RuntimeError('Last.fm qualified plays could not be saved.') from None
                delay = 5
                failed = True
            if (self._closing and not self._pending and not self._forget_requests
                    and self._flight is None):
                return
            if not failed and (self._pending or self._forget_requests or self._wake.is_set()):
                continue
            wake = asyncio.create_task(self._wake.wait(), name='lastfm-worker-wake')
            try:
                waiting = {wake}
                if self._flight is not None and not (failed and self._flight.done()):
                    waiting.add(self._flight)
                await asyncio.wait(waiting, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            finally:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)

    async def close(self):
        if self._closed:
            return
        if self._worker is None:
            self._closed = True
            return
        for play in list(self._plays.values()):
            self._observe(play.session, 'end')
        self._closing = True
        if getattr(self.music, 'playback_observer', None) is self._observer:
            self.music.playback_observer = None
        self._now_playing.clear()
        if self._flight is not None:
            self._flight.cancel()
        self._wake.set()
        try:
            await self._worker
        finally:
            if self._flight is not None:
                self._flight.cancel()
                await asyncio.gather(self._flight, return_exceptions=True)
            for _, future in self._forget_requests:
                if not future.done():
                    future.set_exception(RuntimeError('Last.fm listening storage is closed.'))
            await self._disk(self._store.close)
            self._executor.shutdown(wait=False)
            self._executor = None
            self._closed = True
