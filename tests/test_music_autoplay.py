"""Opt-in radio continuation, explicit queue precedence and safe exhaustion."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from plugins.music.service import MusicError, Track
from test_music import Backend, fixture


def track(title):
    return Track('encoded', title, 'artist',
                 'https://www.youtube.com/watch?v=' + title.ljust(11, 'x')[:11], 120000)


class RadioBackend(Backend):
    def __init__(self):
        super().__init__()
        self.recommendations = AsyncMock(return_value=[track('seed'), track('radio'), track('another')])

    async def load(self, query):
        return [track(query.removeprefix('ytsearch:'))], False


def radio_fixture(tmp_path):
    service, _, message, voice = fixture(tmp_path)
    voice.members = [message.author]
    backend = RadioBackend()
    backend.service = service
    service.backend = backend
    return service, backend, message, voice


def playing(session):
    assert session.current is not None
    return session.current


def test_autoplay_uses_new_tracks_after_explicit_queue_and_skips_recent(tmp_path):
    service, backend, message, _ = radio_fixture(tmp_path)

    async def journey():
        await service.execute(message, 'play', query='seed')
        await service.execute(message, 'autoplay', value=True)
        await service.execute(message, 'play', query='queued')
        session = service.sessions[10]
        await service.ended(10, playing(session).key)
        assert playing(session).title == 'queued'
        await service.ended(10, playing(session).key)
        assert playing(session).title == 'radio'
        assert playing(session).public()['autoplay'] is True
        assert playing(session).requester == message.author.id
        await service.execute(message, 'skip')
        assert playing(session).title == 'another'
        await service.ended(10, playing(session).key)
        assert session.current is None
        assert not session.queue
        await service.close()

    asyncio.run(journey())


def test_repeat_precedes_autoplay_and_stop_cannot_restart_from_stale_end(tmp_path):
    service, backend, message, _ = radio_fixture(tmp_path)

    async def journey():
        await service.execute(message, 'play', query='seed')
        await service.execute(message, 'autoplay', value=True)
        await service.execute(message, 'repeat', value='track')
        session = service.sessions[10]
        await service.ended(10, playing(session).key)
        assert playing(session).title == 'seed'
        await service.execute(message, 'repeat', value='queue')
        await service.ended(10, playing(session).key)
        assert playing(session).title == 'seed'
        old_key = playing(session).key
        message.author.guild_permissions.manage_guild = True
        result = await service.execute(message, 'stop')
        assert result['state']['autoplay'] is False
        await service.ended(10, old_key)
        assert session.current is None
        await service.close()

    asyncio.run(journey())


@pytest.mark.parametrize('reason', ['off', 'disabled', 'empty', 'failed'])
def test_autoplay_does_not_continue_without_opt_in_listeners_or_after_source_failure(tmp_path, reason):
    service, backend, message, voice = radio_fixture(tmp_path)

    async def journey():
        await service.execute(message, 'play', query='seed')
        session = service.sessions[10]
        if reason != 'off':
            await service.execute(message, 'autoplay', value=True)
        if reason == 'disabled':
            await service.execute(message, 'autoplay', value=False)
        if reason == 'empty':
            voice.members = []
        await service.ended(10, playing(session).key, failed=reason == 'failed')
        assert session.current is None
        await service.close()

    asyncio.run(journey())


def test_autoplay_source_failure_goes_idle_without_leaking_error_or_retrying(tmp_path):
    service, backend, message, _ = radio_fixture(tmp_path)
    backend.recommendations.side_effect = OSError('private-source-token')

    async def journey():
        await service.execute(message, 'play', query='seed')
        await service.execute(message, 'autoplay', value=True)
        session = service.sessions[10]
        old_key = playing(session).key
        await service.ended(10, old_key)
        assert session.current is None
        assert 'private-source-token' not in session.error
        await service.ended(10, old_key)
        backend.recommendations.assert_awaited_once()
        await service.close()

    asyncio.run(journey())


def test_autoplay_permissions_boolean_and_guild_isolation(tmp_path):
    service, _, message, _ = radio_fixture(tmp_path)
    _, _, other, _ = fixture(tmp_path, gid=20, uid=200)

    async def journey():
        await service.execute(message, 'play', query='seed')
        await service.execute(other, 'join')
        with pytest.raises(MusicError, match='true or false'):
            await service.execute(message, 'autoplay', value=1)
        message.author.voice.channel = None
        with pytest.raises(MusicError, match='active voice'):
            await service.execute(message, 'autoplay', value=True)
        message.author.guild_permissions.manage_guild = True
        await service.execute(message, 'autoplay', value=True)
        assert (await service.execute(message, 'status'))['autoplay'] is True
        assert (await service.execute(other, 'status'))['autoplay'] is False
        await service.close()

    asyncio.run(journey())


def test_autoplay_resolves_before_end_and_handoff_does_not_search_again(tmp_path):
    service, backend, message, _ = radio_fixture(tmp_path)

    async def journey():
        resolved = asyncio.Event()
        calls = 0

        async def recommend(seed):
            nonlocal calls
            calls += 1
            if seed.title == 'seed':
                if calls != 1:
                    await asyncio.Event().wait()  # A second lookup would stall the handoff.
                resolved.set()
            return [track('radio')]

        backend.recommendations = recommend
        await service.execute(message, 'play', query='seed')
        await service.execute(message, 'autoplay', value=True)
        await asyncio.wait_for(resolved.wait(), 1)
        session = service.sessions[10]
        assert playing(session).title == 'seed'
        await asyncio.wait_for(service.ended(10, playing(session).key), 1)
        assert playing(session).title == 'radio'
        assert playing(session).autoplay
        await service.close()

    asyncio.run(journey())


@pytest.mark.parametrize('action', ['autoplay', 'stop', 'leave', 'repeat'])
def test_pending_radio_cannot_restart_after_opt_out_stop_leave_or_repeat(tmp_path, action):
    service, backend, message, _ = radio_fixture(tmp_path)

    async def journey():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def recommend(seed):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        backend.recommendations = recommend
        await service.execute(message, 'play', query='seed')
        await service.execute(message, 'autoplay', value=True)
        session = service.sessions[10]
        key = playing(session).key
        await asyncio.wait_for(started.wait(), 1)
        message.author.guild_permissions.manage_guild = True
        value = False if action == 'autoplay' else 'track' if action == 'repeat' else None
        await asyncio.wait_for(service.execute(message, action, value=value), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        await service.ended(10, key)
        if action in {'stop', 'leave', 'autoplay'}:
            assert session.current is None
        else:
            assert playing(session).title == 'seed'
        assert all(call[2] != 'radio' for call in backend.calls if call[0] == 'play')
        await service.close()

    asyncio.run(journey())


def test_explicit_queue_wins_over_completed_radio_prefetch(tmp_path):
    service, backend, message, _ = radio_fixture(tmp_path)

    async def journey():
        resolved = asyncio.Event()

        async def recommend(seed):
            resolved.set()
            return [track('radio')]

        backend.recommendations = recommend
        await service.execute(message, 'play', query='seed')
        await service.execute(message, 'autoplay', value=True)
        await asyncio.wait_for(resolved.wait(), 1)
        await service.execute(message, 'play', query='requested')
        session = service.sessions[10]
        await service.ended(10, playing(session).key)
        assert playing(session).title == 'requested'
        await service.close()

    asyncio.run(journey())


def test_prefetch_is_bounded_and_deferred_guild_recovers_capacity(tmp_path):
    service, backend, first, voice = radio_fixture(tmp_path)
    _, _, second, second_voice = fixture(tmp_path, gid=20, uid=200)
    _, _, third, third_voice = fixture(tmp_path, gid=30, uid=300)
    second_voice.members, third_voice.members = [second.author], [third.author]

    async def journey():
        active = 0
        maximum = 0
        started = {uid: asyncio.Event() for uid in (100, 200, 300)}
        release = {uid: asyncio.Event() for uid in (100, 200, 300)}

        async def recommend(seed):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            started[seed.requester].set()
            try:
                await release[seed.requester].wait()
                return [track('radio')]
            finally:
                active -= 1

        backend.recommendations = recommend
        for message in (first, second, third):
            await service.execute(message, 'play', query='seed')
            await service.execute(message, 'autoplay', value=True)
        await asyncio.wait_for(started[100].wait(), 1)
        await asyncio.wait_for(started[200].wait(), 1)
        assert not started[300].is_set()
        await service.execute(first, 'autoplay', value=False)
        # Cancellation callbacks release admission before the next maintenance tick.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await service.tick()
        await asyncio.wait_for(started[300].wait(), 1)
        release[300].set()
        session = service.sessions[30]
        await asyncio.wait_for(service.ended(30, playing(session).key), 1)
        assert playing(session).title == 'radio'
        assert maximum <= 2
        await service.close()

    asyncio.run(journey())
