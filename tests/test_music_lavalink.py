"""Real Lavalink.py client against an in-process v4 REST/WebSocket node."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
import lavalink
import discord
from discord.ext import commands

from plugins.music.backend import LavalinkBackend, ManagedPlayer, LavalinkVoiceProtocol
from plugins.music.service import MusicService, MusicError
from test_music import fixture


def audio_track(title='test song'):
    return {'encoded': 'opaque-track', 'info': {'identifier': 'abcdefghijk', 'isSeekable': True,
        'author': 'artist', 'length': 120000, 'isStream': False, 'position': 0,
        'title': title, 'uri': 'https://www.youtube.com/watch?v=abcdefghijk',
        'sourceName': 'youtube', 'artworkUrl': None}, 'pluginInfo': {}, 'userData': {}}


def test_real_client_voice_handshake_trackstart_and_next(tmp_path, monkeypatch):
    async def journey():
        _, _, msg, voice = fixture(tmp_path)
        sockets, requests = [], []
        ready = asyncio.Event()
        async def socket(request):
            assert request.headers['Authorization'] == 'test-secret'
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            await ws.send_json({'op': 'ready', 'resumed': False, 'sessionId': 'test-session'})
            async for _ in ws:
                pass
            return ws
        async def session(request):
            data = await request.json()
            assert data == {'resuming': True, 'timeout': 120}
            ready.set()
            return web.json_response({'resuming': True, 'timeout': 120})
        async def load(request):
            query = request.query['identifier']
            requests.append(('load', query))
            return web.json_response({'loadType': 'search', 'data': [audio_track(query.removeprefix('ytsearch:'))]})
        async def player(request):
            if request.method == 'DELETE':
                return web.Response(status=204)
            data = await request.json()
            requests.append(('player', data))
            if data.get('track', {}).get('encoded'):
                await sockets[-1].send_json({'op': 'event', 'type': 'TrackStartEvent',
                    'guildId': request.match_info['gid'], 'track': audio_track()})
            return web.json_response({'guildId': request.match_info['gid'], 'track': None,
                'volume': data.get('volume', 50), 'paused': data.get('paused', False),
                'state': {'time': 1, 'position': 0, 'connected': True, 'ping': 0},
                'voice': {}, 'filters': {}})
        app = web.Application()
        app.router.add_get('/v4/websocket', socket)
        app.router.add_patch('/v4/sessions/test-session', session)
        app.router.add_get('/v4/loadtracks', load)
        app.router.add_route('*', '/v4/sessions/test-session/players/{gid}', player)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        monkeypatch.setenv('LAVALINK_PASSWORD', 'test-secret')
        monkeypatch.setenv('LAVALINK_HOST', '127.0.0.1')
        monkeypatch.setenv('LAVALINK_PORT', str(port))
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        await bot._async_setup_hook()
        bot._control = {}
        bot._connection.user = SimpleNamespace(id=777)
        class GuildContext(SimpleNamespace):
            @property
            def voice_client(self):
                return bot._connection._get_voice_client(self.id)
        guild = GuildContext(**vars(msg.guild))
        guild._update_voice_state = lambda data, channel_id: (None, None, None)
        msg.guild, voice.guild = guild, guild
        bot._connection._guilds[guild.id] = guild
        voice._state = bot._connection
        voice._get_voice_client_key = lambda: (guild.id, guild.id)
        voice.connect = lambda **kwargs: discord.abc.Connectable.connect(voice, **kwargs)
        backend = LavalinkBackend(bot)
        service = MusicService(bot, backend, tmp_path / 'settings.json')
        async def change_voice(*, channel, **kwargs):
            gid = str(msg.guild.id)
            bot._connection.parse_voice_state_update({
                'guild_id': gid, 'user_id': '777', 'channel_id': str(channel.id) if channel else None,
                'session_id': 'discord-session'})
            if channel:
                bot._connection.parse_voice_server_update({
                    'guild_id': gid, 'endpoint': 'voice.example.test', 'token': 'discord-secret'})
        msg.guild.change_voice_state = change_voice
        try:
            await backend.start()
            async with asyncio.timeout(3):
                await ready.wait()
            result = await service.execute(msg, 'play', query='first')
            assert result['started']['title'] == 'first'
            voice_payload = next(d['voice'] for kind, d in requests if kind == 'player' and 'voice' in d)
            assert voice_payload['channelId'] == '11'  # Mandatory DAVE field.
            assert voice_payload['sessionId'] == 'discord-session'
            assert isinstance(guild.voice_client, LavalinkVoiceProtocol)
            # Same Discord session ID while moving must still refresh DAVE channelId.
            voice.id = 22
            await backend.join(guild, voice)
            moved = [d['voice'] for kind, d in requests if kind == 'player' and 'voice' in d][-1]
            assert moved['channelId'] == '22'
            await service.execute(msg, 'play', query='second')
            session_state = service.sessions[10]
            advanced = asyncio.Event()
            async def panel(s):
                if s.current and s.current.title == 'second':
                    advanced.set()
            service.panel_update = panel
            await sockets[-1].send_json({'op': 'event', 'type': 'TrackEndEvent', 'guildId': '10',
                                        'reason': 'finished', 'track': audio_track()})
            async with asyncio.timeout(3):
                await advanced.wait()
            assert session_state.current.title == 'second' and not session_state.queue
            assert not backend.pending
            assert isinstance(backend.client.player_manager.get(10), ManagedPlayer)
            # Offline destroy must remove queued reconnect players.
            backend.client.node_manager._player_queue.append(backend.client.player_manager.get(10))
            await service.disconnected(10)
            assert not backend.client.node_manager._player_queue and not service.sessions
        finally:
            await service.close()
            await bot.close()
            await runner.cleanup()
        assert not any(bot.extra_events.values())
    asyncio.run(journey())


def test_missing_audio_configuration_does_not_open_connections(monkeypatch):
    monkeypatch.delenv('LAVALINK_PASSWORD', raising=False)
    backend = LavalinkBackend(SimpleNamespace(user=SimpleNamespace(id=1)))
    with pytest.raises(MusicError, match='not configured'):
        asyncio.run(backend.start())
    assert backend.client is None


def test_failed_start_does_not_resolve_as_success(tmp_path):
    backend = LavalinkBackend(SimpleNamespace())
    async def journey():
        future = asyncio.get_running_loop().create_future()
        backend.pending[10] = ('key', future)
        track = lavalink.AudioTrack(audio_track())
        track.user_data = {'maxwell_track_id': 'key'}
        player = SimpleNamespace(guild_id=10)
        await backend.event(lavalink.TrackExceptionEvent(player, track, 'private error', lavalink.Severity.COMMON, 'private cause', 'private stack'))
        with pytest.raises(MusicError, match='failed to start'):
            await future
    asyncio.run(journey())


def test_managed_player_does_not_auto_advance_queue():
    async def journey():
        player = SimpleNamespace(play=AsyncMock())
        await ManagedPlayer.handle_event(player, SimpleNamespace())
        player.play.assert_not_awaited()
    asyncio.run(journey())


def test_recovery_serializes_with_actions_and_keeps_authoritative_state(tmp_path, monkeypatch):
    async def journey():
        service, _, msg, _ = fixture(tmp_path)
        await service.execute(msg, 'play', query='before outage')
        session = service.sessions[10]
        player = SimpleNamespace(client=SimpleNamespace(music_service=service), guild_id=10,
                                 current=object(), _next=object())
        # super() requires an actual subclass instance; create without transport IO.
        actual = object.__new__(ManagedPlayer)
        actual.client = player.client
        actual.guild_id = 10
        actual.current, actual._next = player.current, player._next
        changed = asyncio.Event()
        async def change(self, node):
            assert session.lock.locked()
            changed.set()
        monkeypatch.setattr(lavalink.DefaultPlayer, 'change_node', change)
        async with session.lock:
            task = asyncio.create_task(actual.change_node(SimpleNamespace()))
            await asyncio.sleep(0)
            assert not changed.is_set()
        await task
        assert session.current.title == 'before outage'
        session.current = None
        await actual.change_node(SimpleNamespace())
        assert actual.current is None and actual._next is None
    asyncio.run(journey())
