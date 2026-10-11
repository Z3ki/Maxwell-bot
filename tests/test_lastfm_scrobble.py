"""Real lifecycle accounting, durable outage recovery and consent-generation fences."""
import asyncio
import sqlite3
import threading
from collections import deque
from types import SimpleNamespace

import pytest

from plugins.lastfm.client import LastFmError
from plugins.lastfm.scrobble import ScrobbleManager
from plugins.lastfm.service import LinkedAccount
from plugins.music.service import Session, Track
from test_music import fixture


def response(accepted=1, ignored=0, code=0):
    return {'scrobbles': {'@attr': {'accepted': str(accepted), 'ignored': str(ignored)},
                         'scrobble': {'ignoredMessage': {'code': str(code)},
                                      'artist': {'#text': 'Unapproved correction', 'corrected': '1'}}}}


class Clock:
    def __init__(self):
        self.mono = 0.0
        self.utc = 1700000000.0

    def advance(self, seconds):
        self.mono += seconds
        self.utc += seconds


class Client:
    def __init__(self):
        self.now = []
        self.scrobbles = []
        self.outcomes = deque()
        self.now_gate = None
        self.scrobble_gate = None
        self.now_error = None
        self.music = None
        self.active = 0
        self.maximum_active = 0
        self.changed = None

    async def update_now_playing(self, key, **kwargs):
        self.now.append((key, kwargs))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.changed is not None:
            self.changed.set()
        try:
            if self.now_gate is not None:
                await self.now_gate.wait()
            if self.now_error is not None:
                raise self.now_error
            return {'nowplaying': {'artist': {'#text': 'Unapproved correction'}}}
        finally:
            self.active -= 1

    async def scrobble(self, key, **kwargs):
        # Starting a Last.fm request must not require/retain music's locks.
        assert all(not session.lock.locked() and not session.play_lock.locked()
                   for session in self.music.sessions.values())
        self.scrobbles.append((key, kwargs))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.changed is not None:
            self.changed.set()
        try:
            if self.scrobble_gate is not None:
                await self.scrobble_gate.wait()
            outcome = self.outcomes.popleft() if self.outcomes else response()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.active -= 1


class Accounts:
    configured = True

    def __init__(self, client):
        self.client = client
        self.links = {'100': LinkedAccount('100', 'fixture-user', 'synthetic-session-one', 'consent-one')}
        self.invalidations = []
        self.scrobbler = None

    def account_for(self, user_id):
        return self.links.get(str(user_id))

    async def invalidate(self, user_id, link_id):
        self.invalidations.append((str(user_id), link_id))
        account = self.account_for(user_id)
        if account is not None and account.link_id == link_id:
            self.links.pop(str(user_id))
            await self.scrobbler.forget_user(user_id)


def harness(tmp_path, *, clock=None, client=None, accounts=None):
    music, backend, message, voice = fixture(tmp_path)
    member = message.author
    member.guild = message.guild
    member.voice.deaf = member.voice.self_deaf = False
    voice.members = [member]
    message.guild.me.voice = SimpleNamespace(channel=voice)
    session = Session(message.guild, channel=voice)
    music.sessions[message.guild.id] = session
    client = client or Client()
    client.music = music
    accounts = accounts or Accounts(client)
    accounts.client = client
    manager = ScrobbleManager(music, accounts, tmp_path / 'private-lastfm' / 'scrobbles.sqlite3')
    changed = asyncio.Event()
    client.changed = changed
    cycle = manager._cycle

    async def observed_cycle():
        try:
            return await cycle()
        finally:
            changed.set()

    manager._cycle = observed_cycle
    accounts.scrobbler = manager
    clock = clock or Clock()
    manager._monotonic = lambda: clock.mono
    manager._clock = lambda: clock.utc
    return SimpleNamespace(manager=manager, music=music, backend=backend, message=message,
                           voice=voice, member=member, session=session, client=client,
                           accounts=accounts, clock=clock, changed=changed)


async def start(h, title='Structured title', duration_ms=120000, *, author='Structured artist',
                seekable=True, requester=100, autoplay=False):
    track = Track('synthetic-encoded', title, author,
                  'https://www.youtube.com/watch?v=abcdefghijk', duration_ms,
                  seekable=seekable, requester=requester, autoplay=autoplay)
    async with h.session.lock:
        await h.music._start(h.session, track)
    return h.session.current


async def until(h, predicate):
    async with asyncio.timeout(3):
        while not predicate():
            h.changed.clear()
            await h.changed.wait()


async def delivered(h, count=1):
    await until(h, lambda: len(h.client.scrobbles) >= count)
    await until(h, lambda: h.manager._flight is None and not h.manager._pending)


async def transition(h, *, channel, deaf=False, self_deaf=False):
    before = h.member.voice
    after = SimpleNamespace(channel=channel, deaf=deaf, self_deaf=self_deaf)
    h.member.voice = after
    h.voice.members = [h.member] if channel is h.voice else []
    await h.manager.voice_state_changed(h.member, before, after)


def rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY timestamp,sequence')]


@pytest.mark.parametrize(('duration_ms', 'threshold'), [(31000, 15.5), (120000, 60), (600000, 240)])
def test_threshold_is_actual_unpaused_time_and_once_per_play(tmp_path, duration_ms, threshold):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        try:
            await start(h, duration_ms=duration_ms)
            h.clock.advance(threshold - .5)
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            assert not h.client.scrobbles
            h.clock.advance(.5)
            await h.manager.tick()
            await delivered(h)
            sent = h.client.scrobbles[0][1]
            assert sent['timestamp'] == 1700000000
            assert sent['duration'] == duration_ms // 1000
            assert sent['artist'] == 'Structured artist'
            assert sent['track'] == 'Structured title'
            assert sent['chosen_by_user'] is True
            h.clock.advance(600)
            await h.manager.tick()
            await h.music.ended(10, h.session.current.key)
            assert len(h.client.scrobbles) == 1
        finally:
            await h.manager.close()
    asyncio.run(journey())


@pytest.mark.parametrize(('duration_ms', 'seekable', 'author'), [
    (0, True, 'Artist'), (-1, True, 'Artist'), (30000, True, 'Artist'),
    (29999, True, 'Artist'), (120000, False, 'Artist'), (120000, True, ''),
])
def test_unknown_short_stream_and_missing_structured_artist_are_ineligible(tmp_path, duration_ms, seekable, author):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        try:
            await start(h, title='Artist - filename.mp3', duration_ms=duration_ms,
                        seekable=seekable, author=author)
            h.clock.advance(1000)
            await h.manager.tick()
            await h.music.ended(10, h.session.current.key)
            assert await h.manager.pending_count(100) == 0
            assert not h.client.now and not h.client.scrobbles
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_pause_deaf_absence_and_seek_never_earn_unheard_time(tmp_path):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(10)
            await h.manager.tick()
            await h.music.execute(h.message, 'seek', value=110)
            assert await h.manager.pending_count(100) == 0
            h.clock.advance(20)
            await h.music.execute(h.message, 'pause')  # 30 listened
            h.clock.advance(500)
            await h.manager.tick()
            await h.music.execute(h.message, 'resume')
            h.clock.advance(10)
            await transition(h, channel=h.voice, self_deaf=True)  # 40 listened
            h.clock.advance(100)
            await h.manager.tick()
            await transition(h, channel=h.voice, deaf=True)  # still deaf
            h.clock.advance(100)
            await transition(h, channel=h.voice)
            h.clock.advance(10)
            await transition(h, channel=None)  # 50 listened; gateway cache is already changed
            h.clock.advance(100)
            await h.manager.tick()
            await transition(h, channel=h.voice)
            h.clock.advance(9)
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            h.clock.advance(1)
            await h.manager.tick()
            await delivered(h)
            await h.music.execute(h.message, 'seek', value=0)
            h.clock.advance(120)
            await h.manager.tick()
            assert len(h.client.scrobbles) == 1
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_linking_during_pause_starts_timestamp_at_first_audible_resume(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.accounts.links.clear()
        await h.manager.start()
        try:
            await start(h)
            await h.music.execute(h.message, 'pause')
            h.clock.advance(20)
            h.accounts.links['100'] = LinkedAccount('100', 'fixture', 'synthetic-session', 'new-consent')
            await h.manager.tick()
            h.clock.advance(100)
            await h.music.execute(h.message, 'resume')
            h.clock.advance(59)
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            h.clock.advance(1)
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][1]['timestamp'] == 1700000120
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_each_listener_starts_at_join_or_link_and_requester_outside_voice_does_not_count(tmp_path):
    async def journey():
        h = harness(tmp_path)
        late = SimpleNamespace(id=200, bot=False, guild=h.message.guild,
                               voice=SimpleNamespace(channel=h.voice, deaf=False, self_deaf=False))
        bot = SimpleNamespace(id=300, bot=True, guild=h.message.guild,
                              voice=SimpleNamespace(channel=h.voice, deaf=False, self_deaf=False))
        h.accounts.links['300'] = LinkedAccount('300', 'fixture-bot', 'synthetic-bot-key', 'bot-link')
        h.voice.members = [bot]
        h.member.voice.channel = None
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(30)
            h.voice.members.append(late)  # joined, but no consent yet
            await h.manager.voice_state_changed(late, SimpleNamespace(channel=None), late.voice)
            h.clock.advance(30)
            await h.manager.tick()
            assert not h.client.now and not h.client.scrobbles
            h.accounts.links['200'] = LinkedAccount('200', 'late-user', 'synthetic-late-key', 'late-link')
            await h.manager.tick()  # linking never backfills the previous 60 seconds
            h.clock.advance(59)
            await h.manager.tick()
            assert await h.manager.pending_count(200) == 0
            h.clock.advance(1)
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][0] == 'synthetic-late-key'
            assert h.client.scrobbles[0][1]['chosen_by_user'] is False
            assert h.client.scrobbles[0][1]['timestamp'] == 1700000060
            assert await h.manager.pending_count(100) == 0
            assert await h.manager.pending_count(300) == 0
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_disconnected_bot_never_reactivates_cached_channel_listeners(tmp_path):
    async def journey():
        h = harness(tmp_path)
        bot = h.message.guild.me
        bot.bot, bot.guild = True, h.message.guild
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(59)
            before = bot.voice
            bot.voice = None  # Discord clears its cached state before transport cleanup.
            await h.manager.voice_state_changed(bot, before, SimpleNamespace(channel=None))
            h.clock.advance(20)
            await h.manager.tick()
            h.clock.advance(20)
            await h.manager.tick()
            assert h.session.current is not None  # Transport deliberately remains stale.
            assert not h.client.scrobbles
            assert await h.manager.pending_count(100) == 0
            bot.voice = SimpleNamespace(channel=h.voice)
            await h.manager.voice_state_changed(bot, SimpleNamespace(channel=None), bot.voice)
            h.clock.advance(1)
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][1]['timestamp'] == 1700000000
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_loading_gaps_end_old_play_before_source_confirmation(tmp_path):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        release = asyncio.Event()
        entered = asyncio.Event()
        original = h.backend.play
        try:
            await start(h, title='first')
            h.clock.advance(59.5)
            h.session.queue.append(Track('encoded-next', 'second', 'Artist', 'youtube:next', 120000))

            async def delayed(*args, **kwargs):
                entered.set()
                await release.wait()
                await original(*args, **kwargs)

            h.backend.play = delayed
            ending = asyncio.create_task(h.music.ended(10, h.session.current.key))
            await entered.wait()
            h.clock.advance(300)
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            release.set()
            await ending
            h.clock.advance(59.5)
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            h.clock.advance(.5)
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][1]['track'] == 'second'
            assert h.client.scrobbles[0][1]['timestamp'] == int(1700000000 + 359.5)
        finally:
            release.set()
            await h.manager.close()
    asyncio.run(journey())


@pytest.mark.parametrize('repeat', ['track', 'queue'])
def test_repeats_are_distinct_even_without_play_generation_changes(tmp_path, repeat):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        try:
            await start(h)
            h.session.repeat = repeat
            generation = h.session.play_generation
            first_key = h.session.current.key
            h.clock.advance(60)
            await h.music.ended(10, first_key)  # qualification exactly at end
            assert h.session.current.key != first_key
            assert h.session.play_generation == generation
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h, 2)
            assert [item[1]['timestamp'] for item in h.client.scrobbles] == [1700000000, 1700000060]
            assert [item[1]['track'] for item in h.client.scrobbles] == ['Structured title'] * 2
            h.clock.advance(1)
            await h.manager.tick()
            assert len(h.client.scrobbles) == 2
        finally:
            await h.manager.close()
    asyncio.run(journey())


@pytest.mark.parametrize('boundary', ['stop', 'leave', 'disconnected', 'replacement'])
def test_terminal_boundaries_charge_once_and_never_credit_following_idle(tmp_path, boundary):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(60)
            h.member.guild_permissions.manage_guild = True
            if boundary in {'stop', 'leave'}:
                await h.music.execute(h.message, boundary)
            elif boundary == 'disconnected':
                await h.music.disconnected(10)
            else:
                await start(h, title='replacement', duration_ms=30000)
            await delivered(h)
            h.clock.advance(1000)
            await h.manager.tick()
            assert len(h.client.scrobbles) == 1
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_provider_stall_does_not_delay_music_or_prevent_durable_persistence(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.now_gate = asyncio.Event()
        await h.manager.start()
        try:
            await start(h)
            await until(h, lambda: len(h.client.now) == 1)
            h.session.repeat = 'track'
            for _ in range(80):
                h.clock.advance(60)
                await asyncio.wait_for(h.music.ended(10, h.session.current.key), .5)
            await until(h, lambda: not h.manager._pending)
            assert await h.manager.pending_count(100) == 80
            saved = await asyncio.to_thread(rows, h.manager._store.path)
            assert len(saved) == 80
            assert len({row['instance_id'] for row in saved}) == 80
            assert h.client.maximum_active == 1
            assert len(h.client.now) == 1 and not h.client.scrobbles
            await asyncio.wait_for(h.music.execute(h.message, 'pause'), .5)
            await asyncio.wait_for(h.manager.close(), 3)
            assert h.music.playback_observer is None
            assert len(await asyncio.to_thread(rows, h.manager._store.path)) == 80
        finally:
            await h.manager.close()
    asyncio.run(journey())
    path = tmp_path / 'private-lastfm' / 'scrobbles.sqlite3'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert b'synthetic-session-one' not in path.read_bytes()


def test_slow_disk_is_off_loop_and_close_drains_qualified_insert(tmp_path):
    async def journey():
        h = harness(tmp_path)
        entered, release = threading.Event(), threading.Event()
        await h.manager.start()
        original = h.manager._store.add

        def slow_add(jobs):
            entered.set()
            release.wait(3)
            return original(jobs)

        h.manager._store.add = slow_add
        h.client.now_gate = asyncio.Event()
        try:
            await start(h)
            h.clock.advance(60)
            await h.manager.tick()
            assert await asyncio.to_thread(entered.wait, 3)
            # Both a confirmed replacement and the Discord-side tick return
            # while the private SQLite executor is demonstrably stalled.
            await asyncio.wait_for(start(h, title='next'), .5)
            await asyncio.wait_for(h.manager.tick(), .5)
            closing = asyncio.create_task(h.manager.close())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 3)
            assert len(await asyncio.to_thread(rows, h.manager._store.path)) == 1
        finally:
            release.set()
            await h.manager.close()
    asyncio.run(journey())


def test_transient_retry_survives_restart_and_sends_cached_plays_oldest_first(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.outcomes.append(LastFmError('Offline', code=16, retryable=True))
        await h.manager.start()
        await start(h, title='oldest')
        h.clock.advance(60)
        await h.manager.tick()
        await delivered(h)
        saved = await asyncio.to_thread(rows, h.manager._store.path)
        assert len(saved) == 1 and saved[0]['attempts'] == 1
        assert await h.manager.pending_count(100) == 1
        h.clock.advance(1)
        await start(h, title='newer')
        h.clock.advance(60)
        # Close synchronously queues the second qualified play and drains it,
        # without waiting for (or dropping) either provider submission.
        await h.manager.close()
        assert len(await asyncio.to_thread(rows, h.manager._store.path)) == 2
        h2 = harness(tmp_path, clock=h.clock)
        await h2.manager.start()
        try:
            await delivered(h2, 2)
            assert [item[1]['track'] for item in h2.client.scrobbles] == ['oldest', 'newer']
            assert await h2.manager.pending_count(100) == 0
            assert not await asyncio.to_thread(rows, h2.manager._store.path)
        finally:
            await h2.manager.close()
    asyncio.run(journey())


@pytest.mark.parametrize('outcome', [response(0, 1, 1), response(0, 1, 3),
                                    {'scrobbles': {'@attr': {'accepted': '1', 'ignored': '0'}}},
                                    LastFmError('Invalid parameters', code=6)])
def test_permanent_rejection_is_removed_without_retry_or_false_success(tmp_path, outcome):
    async def journey():
        h = harness(tmp_path)
        h.client.outcomes.append(outcome)
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h)
            assert await h.manager.pending_count(100) == 0
            assert not await asyncio.to_thread(rows, h.manager._store.path)
            h.clock.advance(3600)
            await h.manager.tick()
            assert len(h.client.scrobbles) == 1
        finally:
            await h.manager.close()
    asyncio.run(journey())


@pytest.mark.parametrize('code', [4, 5])
def test_temporary_ignored_response_retains_qualified_play(tmp_path, code):
    async def journey():
        h = harness(tmp_path)
        h.client.outcomes.append(response(0, 1, code))
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h)
            assert await h.manager.pending_count(100) == 1
            saved = await asyncio.to_thread(rows, h.manager._store.path)
            assert saved[0]['next_attempt'] > h.clock.utc
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_failed_now_playing_is_not_retried_and_corrections_never_become_metadata(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.now_error = LastFmError('Offline', code=16, retryable=True)
        await h.manager.start()
        try:
            await start(h)
            await until(h, lambda: len(h.client.now) == 1 and h.manager._flight is None)
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h)
            assert len(h.client.now) == 1
            assert h.client.scrobbles[0][1]['artist'] == 'Structured artist'
            assert h.client.scrobbles[0][1]['track'] == 'Structured title'
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_unlink_cancels_inflight_and_purges_durable_pending_without_music_delay(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.scrobble_gate = asyncio.Event()
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(60)
            await h.manager.tick()
            await until(h, lambda: len(h.client.scrobbles) == 1)
            h.accounts.links.pop('100')
            await asyncio.wait_for(h.manager.forget_user(100), 3)
            assert await h.manager.pending_count(100) == 0
            assert not await asyncio.to_thread(rows, h.manager._store.path)
            await until(h, lambda: h.manager._flight is None)
            h.clock.advance(600)
            await h.manager.tick()
            assert len(h.client.scrobbles) == 1
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_finish_new_link_before_forget_never_blocks_the_new_consent_generation(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.now_gate = asyncio.Event()
        await h.manager.start()
        try:
            await start(h)
            await until(h, lambda: len(h.client.now) == 1)
            h.clock.advance(60)
            await h.manager.tick()
            await until(h, lambda: not h.manager._pending)
            h.accounts.links['100'] = LinkedAccount('100', 'new-account', 'synthetic-session-two', 'consent-two')
            await h.manager.forget_user(100)  # auth service has already replaced its cache
            h.client.now_gate = None
            await start(h, title='new consent play')
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h)
            assert [key for key, _ in h.client.scrobbles] == ['synthetic-session-two']
            assert h.client.scrobbles[0][1]['track'] == 'new consent play'
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_restart_does_not_borrow_relinked_account_for_old_jobs(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.now_gate = asyncio.Event()
        await h.manager.start()
        await start(h)
        await until(h, lambda: len(h.client.now) == 1)
        h.clock.advance(60)
        await h.manager.tick()
        await h.manager.close()
        h2 = harness(tmp_path, clock=h.clock)
        h2.accounts.links['100'] = LinkedAccount('100', 'other-account', 'synthetic-session-two', 'consent-two')
        await h2.manager.start()
        try:
            await until(h2, lambda: h2.manager._counts.get('100', 0) == 0)
            assert not h2.client.scrobbles
            assert not await asyncio.to_thread(rows, h2.manager._store.path)
        finally:
            await h2.manager.close()
    asyncio.run(journey())


def test_session_revocation_invalidation_can_await_forget_without_worker_deadlock(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.outcomes.append(LastFmError('Reconnect', code=9))
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h)
            assert h.accounts.invalidations == [('100', 'consent-one')]
            assert h.accounts.account_for(100) is None
            assert await h.manager.pending_count(100) == 0
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_late_session_error_cannot_invalidate_a_new_link(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.client.outcomes.append(LastFmError('Reconnect', code=9))
        h.client.scrobble_gate = asyncio.Event()
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(60)
            await h.manager.tick()
            await until(h, lambda: len(h.client.scrobbles) == 1)
            replacement = LinkedAccount('100', 'new-account', 'synthetic-session-two', 'consent-two')
            h.accounts.links['100'] = replacement
            h.client.scrobble_gate.set()
            await until(h, lambda: h.manager._flight is None)
            assert h.accounts.account_for(100) is replacement
            assert h.accounts.invalidations == [('100', 'consent-one')]
            assert h.client.scrobbles[0][0] == 'synthetic-session-one'
            await start(h, title='after reconnect')
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h, 2)
            assert h.client.scrobbles[1][0] == 'synthetic-session-two'
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_account_owner_mismatch_is_never_used_for_another_listener(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.accounts.links['100'] = LinkedAccount('999', 'other-owner', 'synthetic-other-key', 'other-link')
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(1000)
            await h.manager.tick()
            assert not h.client.now and not h.client.scrobbles
            assert await h.manager.pending_count(100) == 0
        finally:
            await h.manager.close()
    asyncio.run(journey())


@pytest.mark.parametrize(('requester', 'autoplay', 'chosen'), [(0, False, None), (100, True, False)])
def test_ambiguous_choice_is_omitted_and_recommendations_are_not_user_chosen(tmp_path, requester, autoplay, chosen):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        try:
            await start(h, requester=requester, autoplay=autoplay)
            h.clock.advance(60)
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][1]['chosen_by_user'] is chosen
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_close_drains_shielded_session_invalidation_before_stopping_supervisor(tmp_path):
    async def journey():
        entered, release = asyncio.Event(), asyncio.Event()
        h = harness(tmp_path)
        original = h.accounts.invalidate

        async def invalidate(user_id, link_id):
            async def operation():
                entered.set()
                await release.wait()
                await original(user_id, link_id)
            operation_task = asyncio.create_task(operation())
            try:
                await asyncio.shield(operation_task)
            except asyncio.CancelledError:
                await operation_task
                raise

        h.accounts.invalidate = invalidate
        h.client.outcomes.append(LastFmError('Reconnect', code=9))
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(60)
            await h.manager.tick()
            await entered.wait()
            closing = asyncio.create_task(h.manager.close())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 3)
            assert h.accounts.account_for(100) is None
            assert not await asyncio.to_thread(rows, h.manager._store.path)
        finally:
            release.set()
            await h.manager.close()
    asyncio.run(journey())


def test_utc_clock_adjustment_does_not_change_monotonic_listening_time(tmp_path):
    async def journey():
        h = harness(tmp_path)
        await h.manager.start()
        try:
            await start(h)
            h.clock.utc += 86400
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            h.clock.mono = 59
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            h.clock.utc -= 172800
            h.clock.mono = 60
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][1]['timestamp'] == 1700000000
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_bot_channel_move_charges_old_state_then_only_actual_new_channel(tmp_path):
    async def journey():
        h = harness(tmp_path)
        other = SimpleNamespace(id=99, members=[], guild=h.message.guild)
        bot = h.message.guild.me
        bot.bot, bot.guild = True, h.message.guild
        bot.voice = SimpleNamespace(channel=h.voice, deaf=False, self_deaf=True)
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(50)
            before = bot.voice
            bot.voice = SimpleNamespace(channel=other, deaf=False, self_deaf=True)
            await h.manager.voice_state_changed(bot, before, bot.voice)
            # The music transport's channel cache deliberately still points at
            # the old channel. Its listeners can no longer hear this source.
            h.clock.advance(100)
            await h.manager.tick()
            assert await h.manager.pending_count(100) == 0
            before = h.member.voice
            h.member.voice = SimpleNamespace(channel=other, deaf=False, self_deaf=False)
            h.voice.members, other.members = [], [h.member]
            await h.manager.voice_state_changed(h.member, before, h.member.voice)
            h.clock.advance(10)
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][1]['timestamp'] == 1700000000
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_linked_late_joiner_has_an_independent_threshold(tmp_path):
    async def journey():
        h = harness(tmp_path)
        late = SimpleNamespace(id=200, bot=False, guild=h.message.guild,
                               voice=SimpleNamespace(channel=None, deaf=False, self_deaf=False))
        h.accounts.links['200'] = LinkedAccount('200', 'late-user', 'synthetic-late-key', 'late-link')
        await h.manager.start()
        try:
            await start(h)
            h.clock.advance(20)
            before = late.voice
            late.voice = SimpleNamespace(channel=h.voice, deaf=False, self_deaf=False)
            h.voice.members.append(late)
            await h.manager.voice_state_changed(late, before, late.voice)
            h.clock.advance(40)
            await h.manager.tick()
            await delivered(h)
            assert h.client.scrobbles[0][0] == 'synthetic-session-one'
            assert await h.manager.pending_count(200) == 0
            h.clock.advance(19)
            await h.manager.tick()
            assert await h.manager.pending_count(200) == 0
            h.clock.advance(1)
            await h.manager.tick()
            await delivered(h, 2)
            assert h.client.scrobbles[1][0] == 'synthetic-late-key'
            assert h.client.scrobbles[1][1]['timestamp'] == 1700000020
        finally:
            await h.manager.close()
    asyncio.run(journey())


def test_unconfigured_manager_creates_no_storage_tasks_or_music_hook(tmp_path):
    async def journey():
        h = harness(tmp_path)
        h.accounts.configured = False
        await h.manager.start()
        await start(h)
        h.clock.advance(600)
        await h.manager.tick()
        assert h.music.playback_observer is None
        assert h.manager._worker is None and h.manager._executor is None
        assert not h.manager._store.path.exists()
        assert await h.manager.pending_count(100) == 0
        await h.manager.close()
    asyncio.run(journey())
