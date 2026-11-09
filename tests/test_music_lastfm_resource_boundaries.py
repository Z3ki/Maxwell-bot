"""Offline regressions for music transport and private Last.fm storage lifetimes."""
import asyncio
import os
import stat
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.lastfm.scrobble import ScrobbleManager
from plugins.lastfm.service import LastFmService
from plugins.music.backend import LavalinkBackend, LavalinkVoiceProtocol
from plugins.music.service import MusicError


def music_bot():
    return SimpleNamespace(user=SimpleNamespace(id=777), add_listener=Mock(), remove_listener=Mock())


def node_client(*, error=None):
    return SimpleNamespace(add_event_hook=Mock(), add_node=Mock(
        return_value=SimpleNamespace(available=True), side_effect=error), close=AsyncMock())


@pytest.mark.parametrize('port', ['', 'not-a-port', '-1', '0', '65536'])
def test_invalid_port_allocates_no_client_and_can_be_corrected(monkeypatch, port):
    async def journey():
        monkeypatch.setenv('LAVALINK_PASSWORD', 'synthetic-node-password')
        monkeypatch.setenv('LAVALINK_PORT', port)
        client = node_client()
        factory = Mock(return_value=client)
        monkeypatch.setattr('plugins.music.backend.MusicClient', factory)
        backend = LavalinkBackend(music_bot())
        try:
            with pytest.raises(MusicError, match='LAVALINK_PORT'):
                await backend.start()
            factory.assert_not_called()
            assert backend.client is None and backend.node is None
            monkeypatch.setenv('LAVALINK_PORT', '2333')
            await backend.start()
            assert backend.client is client
            factory.assert_called_once()
            assert backend.bot.add_listener.call_count == 2
        finally:
            await backend.close()
        client.close.assert_awaited_once()
    asyncio.run(journey())


def test_failed_node_creation_closes_unpublished_client_and_allows_retry(monkeypatch):
    async def journey():
        monkeypatch.setenv('LAVALINK_PASSWORD', 'synthetic-node-password')
        monkeypatch.setenv('LAVALINK_PORT', '2333')
        failed = node_client(error=RuntimeError('synthetic node setup failure'))
        replacement = node_client()
        factory = Mock(side_effect=[failed, replacement])
        monkeypatch.setattr('plugins.music.backend.MusicClient', factory)
        backend = LavalinkBackend(music_bot())
        try:
            with pytest.raises(RuntimeError, match='synthetic node setup failure'):
                await backend.start()
            failed.close.assert_awaited_once()
            assert backend.client is None and backend.node is None
            backend.bot.add_listener.assert_not_called()
            await backend.start()
            assert backend.client is replacement
        finally:
            await backend.close()
        replacement.close.assert_awaited_once()
    asyncio.run(journey())


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_cancelled_voice_handshake_disconnects_and_destroys_player(cleanup_fails):
    async def journey():
        backend = LavalinkBackend(music_bot())
        backend.destroy = AsyncMock()
        requested = asyncio.Event()
        channel = SimpleNamespace(id=11)

        async def change_voice_state(**kwargs):
            if kwargs['channel'] is not None:
                requested.set()
            elif cleanup_fails:
                raise RuntimeError('synthetic disconnect failure')

        guild = SimpleNamespace(id=10, change_voice_state=AsyncMock(side_effect=change_voice_state))
        task = asyncio.create_task(backend.connect_voice(guild, channel))
        await requested.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        backend.destroy.assert_awaited_once_with(10)
        assert guild.change_voice_state.await_args_list[-1].kwargs == {'channel': None}
        assert not backend._voice_events
    asyncio.run(journey())


def test_initial_gateway_failure_retires_handshake_resources():
    async def journey():
        backend = LavalinkBackend(music_bot())
        backend.destroy = AsyncMock()
        guild = SimpleNamespace(id=10, change_voice_state=AsyncMock(
            side_effect=[RuntimeError('synthetic gateway failure'), None]))
        with pytest.raises(RuntimeError, match='synthetic gateway failure'):
            await backend.connect_voice(guild, SimpleNamespace(id=11))
        backend.destroy.assert_awaited_once_with(10)
        assert guild.change_voice_state.await_args_list[-1].kwargs == {'channel': None}
        assert not backend._voice_events
    asyncio.run(journey())


def test_handshake_timeout_preserves_safe_error_and_retires_player():
    async def journey():
        backend = LavalinkBackend(music_bot())
        backend.destroy = AsyncMock()
        guild = SimpleNamespace(id=10, change_voice_state=AsyncMock())
        with pytest.raises(MusicError, match='did not establish'):
            await backend.connect_voice(guild, SimpleNamespace(id=11), timeout=0)
        backend.destroy.assert_awaited_once_with(10)
        assert not backend._voice_events
    asyncio.run(journey())


@pytest.mark.parametrize('has_protocol', [False, True])
def test_failed_disconnection_still_destroys_remote_player(has_protocol):
    async def journey():
        backend = LavalinkBackend(music_bot())
        backend.destroy = AsyncMock()
        guild = SimpleNamespace(id=10)
        if has_protocol:
            guild.voice_client = Mock(spec=LavalinkVoiceProtocol)
            guild.voice_client.disconnect = AsyncMock(side_effect=RuntimeError('disconnect failed'))
        else:
            guild.change_voice_state = AsyncMock(side_effect=RuntimeError('disconnect failed'))
        with pytest.raises(RuntimeError, match='disconnect failed'):
            await backend.leave(guild)
        backend.destroy.assert_awaited_once_with(10)
    asyncio.run(journey())


def account_service(path):
    return LastFmService(path, client=SimpleNamespace(close=AsyncMock()), api_key='a1' * 16,
                         api_secret='b2' * 16, encryption_key='c3' * 32)


@pytest.mark.parametrize('kind', ['file_symlink', 'directory_symlink', 'fifo'])
def test_account_storage_rejects_unsafe_paths_without_touching_targets(tmp_path, kind):
    async def journey():
        private = tmp_path / 'private'
        target = tmp_path / 'unrelated'
        if kind == 'directory_symlink':
            target.mkdir(mode=0o755)
            private.symlink_to(target, target_is_directory=True)
            path = private / 'accounts.sqlite3'
        else:
            private.mkdir()
            path = private / 'accounts.sqlite3'
            if kind == 'file_symlink':
                target.write_bytes(b'unrelated user data')
                target.chmod(0o644)
                path.symlink_to(target)
            else:
                os.mkfifo(path, 0o640)
                target = path
        original_mode = stat.S_IMODE(target.stat().st_mode)
        service = account_service(path)
        try:
            await service.start()
            assert not service.configured
            assert 'storage is unavailable' in service.configuration_message
            assert stat.S_IMODE(target.stat().st_mode) == original_mode
            if kind == 'file_symlink':
                assert target.read_bytes() == b'unrelated user data'
            elif kind == 'directory_symlink':
                assert not path.exists()
        finally:
            await service.close()
    asyncio.run(journey())


def scrobble_manager(path):
    music = SimpleNamespace(playback_observer=None, sessions={})
    accounts = SimpleNamespace(configured=True, account_for=lambda owner: None)
    return ScrobbleManager(music, accounts, path)


def test_cancelled_outbox_start_closes_disk_connection_and_can_retry(tmp_path):
    async def journey():
        manager = scrobble_manager(tmp_path / 'private' / 'scrobbles.sqlite3')
        opened = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_open = manager._store.open

        def blocked_open():
            result = original_open()
            loop.call_soon_threadsafe(opened.set)
            release.wait()
            return result

        manager._store.open = blocked_open
        task = asyncio.create_task(manager.start())
        try:
            async with asyncio.timeout(3):
                await opened.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert manager._store.connection is None
            assert manager._executor is None and not manager._started
            assert manager._worker is None and manager.music.playback_observer is None
            manager._store.open = original_open
            await manager.start()
            assert manager._worker is not None
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await manager.close()
    asyncio.run(journey())


def test_failed_outbox_start_can_retry_after_storage_is_repaired(tmp_path):
    async def journey():
        manager = scrobble_manager(tmp_path / 'private' / 'scrobbles.sqlite3')
        original_open = manager._store.open
        manager._store.open = Mock(side_effect=OSError('synthetic unavailable storage'))
        try:
            with pytest.raises(RuntimeError, match='could not be opened'):
                await manager.start()
            assert manager._executor is None and not manager._started
            manager._store.open = original_open
            await manager.start()
            assert manager._worker is not None
        finally:
            await manager.close()
    asyncio.run(journey())
