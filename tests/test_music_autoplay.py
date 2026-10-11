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
        backend.recommendations.assert_not_awaited()
        await service.ended(10, playing(session).key)
        assert playing(session).title == 'radio'
        assert playing(session).public()['autoplay'] is True
        assert playing(session).requester == message.author.id
        await service.execute(message, 'skip')
        assert playing(session).title == 'another'
        await service.ended(10, playing(session).key)
        assert session.current is None
        assert session.error and 'No new related' in session.error
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
        backend.recommendations.assert_not_awaited()
        await service.execute(message, 'repeat', value='queue')
        await service.ended(10, playing(session).key)
        assert playing(session).title == 'seed'
        backend.recommendations.assert_not_awaited()
        old_key = playing(session).key
        message.author.guild_permissions.manage_guild = True
        result = await service.execute(message, 'stop')
        assert result['state']['autoplay'] is False
        await service.ended(10, old_key)
        assert session.current is None
        backend.recommendations.assert_not_awaited()
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
        backend.recommendations.assert_not_awaited()
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
        assert session.error and 'Autoplay could not find' in session.error
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
