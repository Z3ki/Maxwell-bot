"""DAVE-compatible Lavalink transport; no audio or extractor subprocesses in Maxwell."""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import lavalink

from .service import MusicError, Track

log = logging.getLogger(__name__)


class MusicClient(lavalink.Client):
    """Client subclass carries a reference to the host service."""


class ManagedPlayer(lavalink.DefaultPlayer):
    async def change_node(self, node):
        service = getattr(self.client, 'music_service', None)
        session = service.sessions.get(self.guild_id) if service else None
        if session:
            async with session.lock:
                if service.sessions.get(self.guild_id) is session:
                    if session.current is None:
                        self.current, self._next = None, None
                    await super().change_node(node)
                    session.error = None
                    await service.publish(session)
        else:
            await super().change_node(node)

    async def handle_event(self, event):
        # MusicService exclusively owns queue transitions under its guild lock.
        # Disable DefaultPlayer's second, unsynchronised queue implementation.
        pass


class LavalinkBackend:
    def __init__(self, bot):
        self.bot = bot
        self.service = None
        self.client = None
        self.node = None
        self.pending: dict[int, tuple[str, asyncio.Future]] = {}
        self._init_lock = asyncio.Lock()
        self._voice_events: dict[int, asyncio.Event] = {}
        self._closed = False

    @property
    def available(self):
        return bool(self.node and self.node.available)

    async def start(self):
        async with self._init_lock:
            if self.client or self._closed:
                return
            password = os.getenv('LAVALINK_PASSWORD', '')
            if not password:
                raise MusicError('Music is not configured. The operator must set LAVALINK_PASSWORD and start the audio node.')
            if not getattr(self.bot, 'user', None):
                raise MusicError('Maxwell is not connected to Discord yet.')
            self.client = MusicClient(self.bot.user.id, player=ManagedPlayer,
                                         request_timeout=aiohttp.ClientTimeout(total=15))
            self.client.music_service = self.service
            self.client.add_event_hook(self.event)
            self.node = self.client.add_node(os.getenv('LAVALINK_HOST', '127.0.0.1'),
                                            int(os.getenv('LAVALINK_PORT', '2333')), password, 'us',
                                            name='music', ssl=os.getenv('LAVALINK_TLS', '').lower() == 'true')
            self.bot.add_listener(self.voice_update, 'on_socket_response')
            self.bot.add_listener(self.voice_state, 'on_voice_state_update')
            self.bot.add_listener(self.guild_removed, 'on_guild_remove')

    async def ready(self):
        await self.start()
        if not self.available:
            raise MusicError('The audio node is unavailable. Try again after it reconnects.')

    async def load(self, query):
        await self.ready()
        result = await self.client.get_tracks(query)
        if result.load_type == lavalink.LoadType.ERROR:
            raise MusicError('The source could not load that song. It may be unavailable or restricted.')
        tracks = []
        for item in result.tracks[:100]:
            # Do not expose remote/plugin metadata or non-YouTube stream URLs.
            if item.source_name != 'youtube' or not item.track:
                continue
            uri = f'https://www.youtube.com/watch?v={item.identifier}'
            tracks.append(Track(item.track, item.title, item.author, uri, item.duration,
                                item.artwork_url, item.is_seekable))
        return tracks, result.load_type == lavalink.LoadType.PLAYLIST

    async def join(self, guild, channel):
        await self.ready()
        player = self.client.player_manager.create(guild.id)
        if player.channel_id == channel.id and {'sessionId', 'endpoint', 'token', 'channelId'} <= player._voice_state.keys():
            return
        event = self._voice_events.setdefault(guild.id, asyncio.Event())
        event.clear()
        await guild.change_voice_state(channel=channel, self_deaf=True)
        try:
            async with asyncio.timeout(15):
                await event.wait()
        except TimeoutError:
            await guild.change_voice_state(channel=None)
            await self.destroy(guild.id)
            raise MusicError('Discord did not establish the voice connection. Check Connect and Speak permissions and try again.') from None
        finally:
            self._voice_events.pop(guild.id, None)

    async def voice_update(self, payload):
        if not self.client:
            return
        try:
            await self.client.voice_update_handler(payload)
            if payload.get('t') in {'VOICE_STATE_UPDATE', 'VOICE_SERVER_UPDATE'}:
                gid = int(payload['d']['guild_id'])
                player = self.client.player_manager.get(gid)
                # Lavalink.py sends DAVE's channelId along with endpoint/token/sessionId.
                if player and {'sessionId', 'endpoint', 'token', 'channelId'} <= player._voice_state.keys() and player._voice_state.get('endpoint'):
                    event = self._voice_events.get(gid)
                    if event:
                        event.set()
        except Exception as exc:
            log.warning('Music voice handshake failed type=%s', type(exc).__name__)

    async def voice_state(self, member, before, after):
        if not self.client or not getattr(self.bot, 'user', None) or member.id != self.bot.user.id:
            return
        session = self.service.sessions.get(member.guild.id)
        if after.channel is None and session and session.channel:
            await self.service.disconnected(member.guild.id)
        elif session and after.channel:
            # Server moderator moves are reflected in status and authorization.
            # Do not acquire the service lock: join waits for these gateway events.
            session.channel = after.channel

    async def guild_removed(self, guild):
        await self.service.disconnected(guild.id)

    async def play(self, gid, track, *, volume, position=0):
        await self.ready()
        player = self.client.player_manager.get(gid)
        if not player:
            raise MusicError('The voice connection was lost. Ask me to join again.')
        future = asyncio.get_running_loop().create_future()
        self.pending[gid] = (track.key, future)
        audio = lavalink.AudioTrack({'encoded': track.encoded, 'info': {
            'identifier': track.uri.rsplit('=', 1)[-1], 'isSeekable': track.seekable,
            'author': track.author, 'length': track.duration_ms, 'isStream': not track.seekable,
            'position': 0, 'title': track.title, 'uri': track.uri, 'sourceName': 'youtube',
            'artworkUrl': track.artwork}, 'userData': {'maxwell_track_id': track.key}}, track.requester)
        try:
            # Exactly one in-flight play per guild; TrackStart must arrive before
            # another play can replace the library's _next pointer.
            await player.play(audio, start_time=position, volume=volume, pause=False)
            async with asyncio.timeout(15):
                await future
        except BaseException:
            if not future.done():
                future.cancel()
            # A timed-out start must not begin unexpectedly after an error reply.
            try:
                async with asyncio.timeout(5):
                    await player.stop()
                    player._next = None
            except Exception:
                pass
            raise
        finally:
            self.pending.pop(gid, None)

    async def event(self, event):
        if isinstance(event, lavalink.NodeReadyEvent):
            await event.node.update_session(resuming=True, timeout=120)
            # The client restores players on a fresh session via change_node;
            # resumed sessions keep remote player state. Queue is in MusicService.
            return
        if isinstance(event, lavalink.NodeDisconnectedEvent):
            log.warning('Music audio node disconnected; queues retained')
            return
        player = getattr(event, 'player', None)
        if player is None:
            return
        gid = player.guild_id
        track = getattr(event, 'track', None)
        key = (track.user_data or {}).get('maxwell_track_id') if track else None
        pending = self.pending.get(gid)
        if isinstance(event, lavalink.TrackStartEvent):
            if pending and key == pending[0] and not pending[1].done():
                pending[1].set_result(True)
        elif isinstance(event, (lavalink.TrackExceptionEvent, lavalink.TrackStuckEvent)):
            if pending and key == pending[0] and not pending[1].done():
                pending[1].set_exception(MusicError('The source failed to start this song. Try a different track.'))
            elif key:
                await self.service.ended(gid, key, failed=True)
        elif isinstance(event, lavalink.TrackEndEvent) and event.reason.may_start_next():
            if pending and key == pending[0] and not pending[1].done():
                pending[1].set_exception(MusicError('The song ended before playback could be confirmed.'))
            elif key:
                await self.service.ended(gid, key, failed=event.reason == lavalink.EndReason.LOAD_FAILED)
        elif isinstance(event, lavalink.WebSocketClosedEvent):
            session = self.service.sessions.get(gid)
            if session:
                session.error = 'Discord voice disconnected; reconnecting.'
                try:
                    await session.guild.change_voice_state(channel=session.channel, self_deaf=True)
                except Exception as exc:
                    log.warning('Music voice recovery failed guild=%s type=%s', gid, type(exc).__name__)

    def position(self, gid):
        player = self.client.player_manager.get(gid) if self.client else None
        return player.position if player else 0

    async def _player(self, gid):
        await self.ready()
        player = self.client.player_manager.get(gid)
        if not player:
            raise MusicError('The voice session is unavailable.')
        return player

    async def pause(self, gid, paused):
        await (await self._player(gid)).set_pause(paused)

    async def volume(self, gid, volume):
        await (await self._player(gid)).set_volume(volume)

    async def seek(self, gid, position):
        await (await self._player(gid)).seek(position)

    async def stop(self, gid):
        await (await self._player(gid)).stop()

    async def destroy(self, gid):
        if self.client:
            # Do not resurrect a destroyed player from the client's reconnect queue.
            self.client.node_manager._player_queue[:] = [
                p for p in self.client.node_manager._player_queue if p.guild_id != gid]
            # PlayerManager removes local state even when the node is offline.
            try:
                await self.client.player_manager.destroy(gid)
            except Exception as exc:
                log.warning('Music player cleanup failed guild=%s type=%s', gid, type(exc).__name__)

    async def leave(self, guild):
        await guild.change_voice_state(channel=None)
        await self.destroy(guild.id)

    async def close(self):
        self._closed = True
        if self.client:
            self.bot.remove_listener(self.voice_update, 'on_socket_response')
            self.bot.remove_listener(self.voice_state, 'on_voice_state_update')
            self.bot.remove_listener(self.guild_removed, 'on_guild_remove')
            for _, future in self.pending.values():
                if not future.done():
                    future.cancel()
            await self.client.close()
            self.client = None
