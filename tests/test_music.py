"""Shared-service tests: permissions, ordering, failures, guild isolation and UI."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from jsonschema import validate, ValidationError

from plugins.music.service import MusicError, MusicService, Track, identifier
from plugins.music.tools import MusicControlTool, MusicInfoTool, perform
from plugins.music.ui import install, uninstall, update_panel, command_definition, format_result


class Backend:
    available = True
    def __init__(self):
        self.calls = []
        self.fail = None
        self.service = None

    async def load(self, query):
        self.calls.append(('load', query))
        if query.endswith('missing'):
            return [], False
        titles = ['playlist A', 'playlist B', 'playlist C'] if 'playlist?' in query else [query.removeprefix('ytsearch:')]
        return [Track('PRIVATE_ENCODED', t, 'artist', 'https://www.youtube.com/watch?v=abcdefghijk', 120000) for t in titles], len(titles) > 1

    async def join(self, guild, channel):
        self.calls.append(('join', guild.id, channel.id))
        if not self.available:
            raise MusicError('The audio node is unavailable.')

    async def play(self, gid, track, **kwargs):
        self.calls.append(('play', gid, track.title))
        if self.fail:
            raise self.fail
        # Yield to exercise actual concurrent requests across guild locks.
        await asyncio.sleep(0)

    async def pause(self, gid, paused):
        self.calls.append(('pause', gid, paused))
    async def volume(self, gid, volume):
        self.calls.append(('volume', gid, volume))
    async def seek(self, gid, position):
        self.calls.append(('seek', gid, position))
    async def stop(self, gid):
        self.calls.append(('stop', gid))
    async def leave(self, guild):
        self.calls.append(('leave', guild.id))
    async def destroy(self, gid):
        self.calls.append(('destroy', gid))
    async def close(self):
        self.calls.append(('close',))
    def position(self, gid):
        return 12000


def fixture(tmp_path, gid=10, uid=100):
    guild = SimpleNamespace(id=gid, owner_id=999, me=SimpleNamespace(id=777), voice_channels=[])
    voice = Mock(spec=discord.VoiceChannel)
    voice.id, voice.name, voice.guild = gid + 1, 'Lounge', guild
    voice.members, voice.user_limit = [], 0
    voice.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True, move_members=False)
    guild.voice_channels.append(voice)
    member = SimpleNamespace(id=uid, bot=False, voice=SimpleNamespace(channel=voice), roles=[],
                             guild_permissions=SimpleNamespace(manage_guild=False, administrator=False))
    guild.get_member = lambda n: member if n == uid else None
    guild.get_channel = lambda n: next((v for v in guild.voice_channels if v.id == n), None)
    guild.get_role = lambda n: SimpleNamespace(id=n) if n == 555 else None
    text = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=55, edit=AsyncMock())))
    message = SimpleNamespace(guild=guild, author=member, channel=text)
    bot = SimpleNamespace(_control={})
    backend = Backend()
    service = MusicService(bot, backend, tmp_path / 'settings.json')
    return service, backend, message, voice


@pytest.mark.parametrize('url', ['http://youtube.com/watch?v=abcdefghijk', 'https://127.0.0.1/a.mp3',
    'file:///etc/passwd', 'https://youtube.com.evil.org/watch?v=abcdefghijk', 'ytsearch:secret',
    'https://user:pw@youtube.com/watch?v=abcdefghijk', 'https://youtube.com:8888/watch?v=abcdefghijk',
    'https://youtube.com/redirect?q=http://localhost', 'https://example.org/a.mp3', ''])
def test_music_rejects_arbitrary_urls(url):
    with pytest.raises((MusicError, ValueError)):
        identifier(url)


@pytest.mark.parametrize('url', ['https://youtu.be/abcdefghijk', 'https://music.youtube.com/watch?v=abcdefghijk',
                                 'https://www.youtube.com/shorts/abcdefghijk', 'youtube.com/watch?v=abcdefghijk'])
def test_normalizes_youtube_links(url):
    assert identifier(url) == 'https://www.youtube.com/watch?v=abcdefghijk'


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=abcdefghijk&list=RDabcdefghijk&start_radio=1',
    'https://youtu.be/abcdefghijk?list=PLtest1234567890',
    'https://music.youtube.com/watch?v=abcdefghijk&list=PLtest1234567890',
    'https://www.youtube.com/shorts/abcdefghijk?list=PLtest1234567890',
    'https://www.youtube.com/live/abcdefghijk?list=PLtest1234567890',
])
def test_video_links_keep_the_video_even_with_playlist_parameters(url):
    assert identifier(url) == 'https://www.youtube.com/watch?v=abcdefghijk'


@pytest.mark.parametrize('path', ['/playlist', '/watch'])
def test_explicit_playlist_only_links_still_load_playlists(path):
    assert identifier('https://www.youtube.com' + path + '?list=PLtest1234567890') == 'https://www.youtube.com/playlist?list=PLtest1234567890'


@pytest.mark.parametrize('path', ['/watch?v=bad&', '/shorts/bad?', '/unknown?'])
def test_invalid_video_or_unknown_path_cannot_silently_load_a_playlist(path):
    with pytest.raises(MusicError, match='not a supported'):
        identifier('https://www.youtube.com' + path + 'list=PLtest1234567890')


def test_watch_mix_queues_one_track_and_status_reports_playback(tmp_path):
    service, backend, msg, _ = fixture(tmp_path)

    async def journey():
        await service.execute(msg, 'play', query='https://www.youtube.com/watch?v=abcdefghijk&list=RDabcdefghijk')
        assert backend.calls[0] == ('load', 'https://www.youtube.com/watch?v=abcdefghijk')
        assert not service.sessions[10].queue
        status = format_result(await service.execute(msg, 'status'))
        assert 'Playing' in status and 'Lounge' in status and '0:12' in status
        assert 'Volume 50%' in status and 'Repeat off' in status and '0 queued' in status
        assert 'queue is empty' not in status
        await service.execute(msg, 'pause')
        assert 'Paused' in format_result(await service.execute(msg, 'status'))
        assert format_result(await service.execute(msg, 'queue')) == 'The queue is empty.'

    asyncio.run(journey())


def test_idle_status_and_queue_have_distinct_responses(tmp_path):
    service, backend, msg, _ = fixture(tmp_path)

    async def journey():
        status = format_result(await service.execute(msg, 'status'))
        assert 'Disconnected' in status and 'Nothing is playing' in status
        assert format_result(await service.execute(msg, 'queue')) == 'The queue is empty.'
        with pytest.raises(MusicError, match='offset'):
            await service.execute(msg, 'queue', offset=-1)
        backend.available = False
        assert 'audio node is unavailable' in format_result(await service.execute(msg, 'status'))

    asyncio.run(journey())


def test_auto_join_queue_next_now_and_stale_events(tmp_path):
    service, backend, msg, voice = fixture(tmp_path)
    async def journey():
        first = await service.execute(msg, 'play', query='first')
        assert first['started']['title'] == 'first'
        session = service.sessions[10]
        assert session.channel is voice
        old = session.current.key
        await service.execute(msg, 'play', query='last')
        await service.execute(msg, 'play', query='next', mode='next')
        assert [t.title for t in session.queue] == ['next', 'last']
        await service.execute(msg, 'play', query='instead', mode='now')
        await service.ended(10, old)
        assert session.current.title == 'instead'
        await service.execute(msg, 'skip')
        assert session.current.title == 'next'
        await service.ended(10, session.current.key)
        assert session.current.title == 'last'
        await service.ended(10, session.current.key)
        assert session.current is None
        assert not session.queue
        assert 'PRIVATE_ENCODED' not in json.dumps(first)
    asyncio.run(journey())
    assert sum(c[0] == 'join' for c in backend.calls) == 1


def test_voice_selection_permissions_and_no_hijacking(tmp_path):
    service, backend, msg, voice = fixture(tmp_path)
    async def journey():
        msg.author.voice.channel = None
        with pytest.raises(MusicError, match='Which voice'):
            await service.execute(msg, 'play', query='song')
        assert not service.sessions
        msg.author.voice.channel = voice
        await service.execute(msg, 'play', query='song')
        other = Mock(spec=discord.VoiceChannel)
        other.id, other.name, other.guild, other.members, other.user_limit = 99, 'Other', msg.guild, [], 0
        other.permissions_for.return_value = voice.permissions_for.return_value
        msg.guild.voice_channels.append(other)
        msg.author.voice.channel = other
        for action in ['play', 'pause', 'skip', 'move']:
            with pytest.raises(MusicError, match='active voice|manager'):
                await service.execute(msg, action, query='song2', channel='Other')
        msg.author.guild_permissions.manage_guild = True
        await service.execute(msg, 'move', channel='Other')
        assert service.sessions[10].channel is other
        other.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=False)
        with pytest.raises(MusicError, match='Speak'):
            service.voice_channel(msg.guild, msg.author, 'Other')
    asyncio.run(journey())


def test_default_channel_requires_authorization_and_ambiguous_names(tmp_path):
    service, _, msg, voice = fixture(tmp_path)
    async def journey():
        msg.author.guild_permissions.manage_guild = True
        await service.execute(msg, 'configure', settings={'default_channel': '11'})
        msg.author.voice.channel = None
        await service.execute(msg, 'join')
        assert service.sessions[10].channel is voice
        twin = Mock(spec=discord.VoiceChannel)
        twin.id, twin.name = 22, voice.name
        msg.guild.voice_channels.append(twin)
        with pytest.raises(MusicError, match='Several'):
            service.voice_channel(msg.guild, msg.author, 'Lounge')
        voice.guild = SimpleNamespace(id=123)
        with pytest.raises(MusicError, match='this server'):
            service.voice_channel(msg.guild, msg.author, '11')
    asyncio.run(journey())


def test_parallel_queue_requests_and_guild_isolation(tmp_path):
    service, backend, msg, _ = fixture(tmp_path)
    _, _, other_msg, _ = fixture(tmp_path, gid=20, uid=200)
    async def journey():
        await asyncio.gather(service.execute(msg, 'play', query='start'), service.execute(other_msg, 'play', query='other start'))
        await asyncio.gather(*(service.execute(msg, 'play', query=f'song{i}') for i in range(6)))
        assert [t.title for t in service.sessions[10].queue] == [f'song{i}' for i in range(6)]
        assert not service.sessions[20].queue
        assert service.sessions[20].current.title == 'other start'
        await service.ended(20, service.sessions[10].current.key)
        assert service.sessions[20].current.title == 'other start'
    asyncio.run(journey())
    assert len([c for c in backend.calls if c[0] == 'play']) == 2


def test_limits_atomic_batch_and_queue_ownership(tmp_path):
    service, _, msg, _ = fixture(tmp_path)
    async def journey():
        service.settings['10'] = {'queue_limit': 3, 'user_queue_limit': 2}
        await service.execute(msg, 'play', query='start')
        await service.execute(msg, 'play', queries=['a', 'b'])
        with pytest.raises(MusicError, match='allowance'):
            await service.execute(msg, 'play', query='c')
        session = service.sessions[10]
        assert [t.title for t in session.queue] == ['a', 'b']
        with pytest.raises(MusicError, match='No playable'):
            await service.execute(msg, 'play', queries=['valid', 'missing'])
        assert len(session.queue) == 2
        session.queue[0].requester = 987
        with pytest.raises(MusicError, match='own songs'):
            await service.execute(msg, 'remove', track_id=session.queue[0].key)
        await service.execute(msg, 'remove', track_id=session.queue[1].key)
        assert len(session.queue) == 1
        msg.author.guild_permissions.manage_guild = True
        await service.execute(msg, 'remove', track_id=session.queue[0].key)
        assert not session.queue
    asyncio.run(journey())


def test_failure_is_not_reported_as_started_and_queue_survives(tmp_path):
    service, backend, msg, _ = fixture(tmp_path)
    async def journey():
        await service.execute(msg, 'play', query='first')
        await service.execute(msg, 'play', query='next')
        backend.fail = OSError('token=PRIVATE')
        result = await perform(service, msg, 'skip')
        assert not result['succeeded'] and 'PRIVATE' not in str(result)
        assert service.sessions[10].current is None
        assert len(service.sessions[10].queue) == 1
        backend.fail = None
        await service.execute(msg, 'skip')
        assert service.sessions[10].current.title == 'next'
        backend.available = False
        result = await service.execute(msg, 'status')
        assert not result['audio_available']
    asyncio.run(journey())


def test_ai_tools_schemas_and_permissions(tmp_path):
    service, _, msg, _ = fixture(tmp_path)
    control, info = MusicControlTool(service.bot, service), MusicInfoTool(service.bot, service)
    for tool in (control, info):
        with pytest.raises(ValidationError):
            validate({'action': 'status', 'guild_id': '20'}, tool.get_parameters())
    async def journey():
        result = json.loads(await control.execute(msg, action='play', query='song'))
        assert result['started']
        assert json.loads(await info.execute(msg, action='status'))['current']
        assert 'error' in json.loads(await control.execute(msg, action='stop'))
        msg.author.guild_permissions.manage_guild = True
        assert json.loads(await control.execute(msg, action='stop'))['stopped']
        assert json.loads(await info.execute(SimpleNamespace(guild=None, author=msg.author), action='status'))['error']
    asyncio.run(journey())
    assert control.is_destructive and not info.side_effects


def test_playlists_repeat_pause_seek_shuffle_and_cleanup(tmp_path):
    service, backend, msg, _ = fixture(tmp_path)
    async def journey():
        msg.author.guild_permissions.manage_guild = True
        await service.execute(msg, 'play', query='https://youtube.com/playlist?list=PLabcdefghijk')
        session = service.sessions[10]
        assert session.current.title == 'playlist A' and len(session.queue) == 2
        await service.execute(msg, 'repeat', value='track')
        await service.ended(10, session.current.key)
        assert session.current.title == 'playlist A' and len(session.queue) == 2
        await service.execute(msg, 'pause')
        assert session.paused
        await service.execute(msg, 'resume')
        await service.execute(msg, 'volume', value=10)
        await service.execute(msg, 'seek', value=30)
        await service.execute(msg, 'repeat', value='queue')
        await service.execute(msg, 'skip')
        assert session.current.title == 'playlist B'
        assert [t.title for t in session.queue] == ['playlist C', 'playlist A']
        # Directly test helper to avoid this journey's user rate limit.
        session.last_active = asyncio.get_running_loop().time() - 3600
        session.empty_since = session.last_active
        await service.tick()
        assert not service.sessions
        await service.close()
    asyncio.run(journey())
    assert ('seek', 10, 30000) in backend.calls and ('close',) in backend.calls


def test_config_persists_and_restricts_listener_controls(tmp_path):
    service, _, msg, _ = fixture(tmp_path)
    async def journey():
        with pytest.raises(MusicError, match='server managers'):
            await service.execute(msg, 'configure', settings={'idle_seconds': 30})
        msg.author.guild_permissions.manage_guild = True
        assert (await service.execute(msg, 'configure', settings={'listener_controls': False, 'dj_role': '555'}))['saved']
        reloaded = MusicService(service.bot, Backend(), service.path)
        await reloaded.load_settings()
        assert reloaded.config(10)['dj_role'] == '555'
        await service.execute(msg, 'play', query='first')
        msg.author.guild_permissions.manage_guild = False
        with pytest.raises(MusicError, match='restricted'):
            await service.execute(msg, 'skip')
        await service.execute(msg, 'play', query='second')
        msg.author.roles = [SimpleNamespace(id=555)]
        await service.execute(msg, 'skip')
        assert service.sessions[10].current.title == 'second'
    asyncio.run(journey())


def test_resource_admission_and_disconnect(tmp_path):
    service, _, msg, _ = fixture(tmp_path)
    async def journey():
        service.max_sessions = 1
        await service.execute(msg, 'join')
        _, _, other, _ = fixture(tmp_path, gid=20, uid=200)
        with pytest.raises(MusicError, match='sessions'):
            await service.execute(other, 'join')
        service.sessions[10].inflight = 8
        with pytest.raises(MusicError, match='busy'):
            await service.execute(msg, 'status')
        service.sessions[10].inflight = 0
        await service.disconnected(10)
        assert not service.sessions
        await service.execute(other, 'join')
        assert 20 in service.sessions
    asyncio.run(journey())


def test_manual_and_ai_share_state_with_panel_validation(tmp_path):
    import user_install as ui
    service, backend, msg, _ = fixture(tmp_path)
    async def journey():
        install(service.bot, service)
        try:
            handler = next(h for _, name, h in ui._INTERACTION_HANDLERS if name == 'music')
            interaction = SimpleNamespace(guild=msg.guild, user=msg.author, channel=msg.channel,
                response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
                data={'name': 'music', 'options': [{'name': 'play', 'options': [{'name': 'query', 'value': 'manual song'}]}]})
            assert await handler(service.bot, interaction)
            interaction.response.defer.assert_awaited_once_with(ephemeral=True)
            assert json.loads(await MusicInfoTool(service.bot, service).execute(msg, action='status'))['current']['title'] == 'manual song'
            session = service.sessions[10]
            panel = session.panel
            await update_panel(session)
            assert session.panel is panel and msg.channel.send.await_count == 1
            await MusicControlTool(service.bot, service).execute(msg, action='play', query='AI next')
            interaction.data = {'custom_id': 'maxwell_music:10:skip'}
            interaction.message = panel
            assert await handler(service.bot, interaction)
            assert session.current.title == 'AI next'
            interaction.data = {'custom_id': 'maxwell_music:20:stop'}
            assert await handler(service.bot, interaction)
            assert session.current.title == 'AI next'
            interaction.message = SimpleNamespace(id=999)
            interaction.data = {'custom_id': 'maxwell_music:10:skip'}
            assert await handler(service.bot, interaction)
            assert session.current.title == 'AI next'
        finally:
            uninstall()
    asyncio.run(journey())
    assert sum(c[0] == 'play' for c in backend.calls) == 2
    assert command_definition()['integration_types'] == [0]


def test_repeat_generates_distinct_playback_ids_and_ignores_duplicate_end(tmp_path):
    service, _, msg, _ = fixture(tmp_path)
    async def journey():
        await service.execute(msg, 'play', query='repeat song')
        await service.execute(msg, 'repeat', value='track')
        session = service.sessions[10]
        old_key = session.current.key
        await service.ended(10, old_key)
        new_key = session.current.key
        assert old_key != new_key
        await service.ended(10, old_key)
        assert session.current.key == new_key
    asyncio.run(journey())


def test_read_state_does_not_reveal_hidden_voice_channels(tmp_path):
    service, _, msg, voice = fixture(tmp_path)
    async def journey():
        await service.execute(msg, 'play', query='private song')
        voice.permissions_for.return_value.view_channel = False
        for action in ('status', 'queue'):
            with pytest.raises(MusicError, match='access'):
                await service.execute(msg, action)
    asyncio.run(journey())
