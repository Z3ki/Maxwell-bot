"""Behavior regressions for real encrypted consent and signed fake-transport HTTP."""
import asyncio
from collections import deque
import hashlib
import json
import os
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import aiohttp
import discord
import pytest
import user_install

from plugins.lastfm.client import API_URL, LastFmClient, LastFmError, signature
from plugins.lastfm.service import LastFmService
from conversation_memory import ConversationMemoryManager
from utils import render_discord_context_text
from plugins.lastfm.tools import LastFmAccountTool
from plugins.lastfm.ui import install, uninstall

KEY = 'a1' * 16
SECRET = 'b2' * 16
ENCRYPTION = 'c3' * 32
TOKEN = 'g7Hz' * 8
SESSION = 'j8Kn' * 8


class FakeResponse:
    def __init__(self, transport, status, payload):
        self.transport, self.status = transport, status
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.content = self

    async def __aenter__(self):
        self.transport.active += 1
        self.transport.max_active = max(self.transport.active, self.transport.max_active)
        return self

    async def __aexit__(self, *args):
        self.transport.active -= 1

    async def iter_chunked(self, size):
        await asyncio.sleep(self.transport.delay)
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]


class FakeTransport:
    """An aiohttp-compatible transport, not an echo of client return values."""
    def __init__(self, replies, delay=0):
        self.replies = deque(replies)
        self.requests = []
        self.active = self.max_active = 0
        self.delay = delay
        self.started = asyncio.Event()

    def request(self, verb, url, **kwargs):
        self.requests.append((time.monotonic(), verb, url, kwargs))
        self.started.set()
        status, payload = self.replies.popleft()
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(self, status, payload)

    async def wait_started(self, count):
        async with asyncio.timeout(3):
            while len(self.requests) < count:
                self.started.clear()
                await self.started.wait()


def make_service(path, replies, *, clock=time.time, encryption_key=ENCRYPTION, delay=0):
    transport = FakeTransport(replies, delay)
    client = LastFmClient(KEY, SECRET, session=transport, requests_per_second=10000)
    return LastFmService(path, client=client, api_key=KEY, api_secret=SECRET,
                         encryption_key=encryption_key, clock=clock), transport


def token_reply(token=TOKEN):
    return 200, {'token': token}


def session_reply(username='private-name', key=SESSION):
    return 200, {'session': {'name': username, 'key': key, 'subscriber': 0}}


def test_signature_uses_ascii_parameter_order_utf8_and_exclusions():
    values = {'track': 'été', 'artist': 'Björk', 'api_key': KEY, 'method': 'track.scrobble',
              'chosenByUser': '0', 'timestamp': '123', 'api_sig': 'excluded',
              'callback': 'excluded', 'format': 'json', 'sk': SESSION}
    expected = ('api_key' + KEY + 'artistBjörkchosenByUser0methodtrack.scrobble'
                + 'sk' + SESSION + 'timestamp123trackété' + SECRET)
    assert signature(values, SECRET) == hashlib.md5(expected.encode('utf-8')).hexdigest()


def test_signed_http_methods_and_ignored_acknowledgements():
    async def run():
        ignored = {'scrobbles': {'@attr': {'accepted': '0', 'ignored': '1'},
                   'scrobble': {'ignoredMessage': {'code': '3', '#text': 'too old'}}}}
        transport = FakeTransport([token_reply(), session_reply(),
            (200, {'nowplaying': {'ignoredMessage': {'code': '0'}}}), (200, ignored)])
        client = LastFmClient(KEY, SECRET, session=transport, requests_per_second=10000)
        assert await client.get_token() == TOKEN
        assert await client.get_session(TOKEN) == ('private-name', SESSION)
        await client.update_now_playing(SESSION, artist='Björk', track='été', duration=300)
        result = await client.scrobble(SESSION, artist='Björk', track='été', duration=300,
                                      timestamp=1700000000, chosen_by_user=False)
        assert result['scrobbles']['@attr']['accepted'] == '0'
        assert [request[1] for request in transport.requests] == ['GET', 'GET', 'POST', 'POST']
        for _, verb, url, kwargs in transport.requests:
            assert url == API_URL and kwargs['allow_redirects'] is False
            assert kwargs['timeout'].total == 15
            params = kwargs['data' if verb == 'POST' else 'params']
            signed = ''.join(k + str(params[k]) for k in sorted(params) if k != 'api_sig' and k != 'format') + SECRET
            assert params['api_sig'] == hashlib.md5(signed.encode()).hexdigest()
        assert 'chosenByUser' not in transport.requests[2][3]['data']
        assert transport.requests[3][3]['data']['chosenByUser'] == '0'
        await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('status,payload,code,retryable', [
    (200, {'error': 14, 'message': TOKEN}, 14, False),
    (200, {'error': 9, 'message': SESSION}, 9, False),
    (503, {'error': 11, 'message': SECRET}, 11, True),
    (200, {'error': 16, 'message': SECRET}, 16, True),
    (429, {'error': 29, 'message': SECRET}, 29, True),
    (503, b'private debug payload', 0, True),
    (429, b'private debug payload', 29, True),
    (200, b'not json', 0, False),
    (200, b'x' * 65537, 0, False),
    (302, b'private debug payload', 0, False),
    (200, aiohttp.ClientConnectionError(TOKEN), 0, True),
])
def test_http_and_provider_failures_are_safe(status, payload, code, retryable):
    async def run():
        client = LastFmClient(KEY, SECRET, session=FakeTransport([(status, payload)]))
        with pytest.raises(LastFmError) as caught:
            await client.get_token()
        assert caught.value.code == code and caught.value.retryable is retryable
        text = str(caught.value)
        assert all(secret not in text for secret in (TOKEN, SESSION, SECRET, 'private debug payload'))
        assert caught.value.__cause__ is None
        await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('payload', [
    {}, {'scrobbles': {'@attr': {'accepted': '1', 'ignored': '1'}, 'scrobble': {'ignoredMessage': {'code': '0'}}}},
    {'scrobbles': {'@attr': {'accepted': '1', 'ignored': '0'}, 'scrobble': {'ignoredMessage': {'code': '3'}}}},
])
def test_scrobble_requires_real_single_track_acknowledgement(payload):
    async def run():
        client = LastFmClient(KEY, SECRET, session=FakeTransport([(200, payload)]))
        with pytest.raises(LastFmError, match='acknowledgement'):
            await client.scrobble(SESSION, artist='artist', track='title', duration=120, timestamp=1700000000)
        await client.close()
    asyncio.run(run())


def test_client_paces_starts_and_bounds_concurrency():
    async def run():
        transport = FakeTransport([token_reply()] * 6, delay=0.08)
        client = LastFmClient(KEY, SECRET, session=transport, requests_per_second=40, concurrency=2)
        await asyncio.gather(*(client.get_token() for _ in range(6)))
        starts = [request[0] for request in transport.requests]
        assert all(after - before >= 0.024 for before, after in zip(starts, starts[1:]))
        assert transport.max_active == 2
        await client.close()
    asyncio.run(run())


def test_encrypted_roundtrip_and_private_permissions(tmp_path):
    path = tmp_path / 'private' / 'accounts.sqlite3'
    async def run():
        service, transport = make_service(path, [token_reply(), session_reply()])
        await service.start()
        url, nonce = await service.connect(100)
        assert parse_qs(urlsplit(url).query)['token'] == [TOKEN]
        stored = path.read_bytes()
        assert TOKEN.encode() not in stored and nonce.encode() not in stored
        await service.close()
        restored, _ = make_service(path, [session_reply()])
        await restored.start()
        account = await restored.finish('100', nonce)
        assert account.username == 'private-name' and account.session_key == SESSION
        assert SESSION not in repr(account)
        assert restored.account_for(200) is None
        assert restored.has_accounts
        persisted_link = account.link_id
        await restored.close()
        persisted, _ = make_service(path, [])
        await persisted.start()
        assert persisted.account_for(100).link_id == persisted_link
        assert persisted.account_for(100).session_key == SESSION
        await persisted.close()
    asyncio.run(run())
    data = path.read_bytes()
    assert all(value.encode() not in data for value in (TOKEN, SESSION, 'private-name'))
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_owner_nonce_expiry_and_single_concurrent_redemption(tmp_path):
    async def run():
        now = [1000.0]
        service, transport = make_service(tmp_path / 'accounts.sqlite3',
            [token_reply(), session_reply(), token_reply()], clock=lambda: now[0], delay=0.01)
        await service.start()
        _, nonce = await service.connect(100)
        with pytest.raises(LastFmError):
            await service.finish(200, nonce)
        with pytest.raises(LastFmError, match='no longer valid'):
            await service.finish(100, nonce + 'x')
        results = await asyncio.gather(service.finish(100, nonce), service.finish(100, nonce), return_exceptions=True)
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(request[3].get('params', {}).get('method') == 'auth.getSession' for request in transport.requests) == 1
        _, expiring = await service.connect(200)
        now[0] += 3600
        with pytest.raises(LastFmError) as caught:
            await service.finish(200, expiring)
        assert caught.value.code == 15
        assert len(transport.requests) == 3
        await service.close()
    asyncio.run(run())


def test_expiry_maintenance_preserves_concurrently_renewed_approval(tmp_path):
    async def run():
        now = [1000.0]
        path = tmp_path / 'accounts.sqlite3'
        service, transport = make_service(path,
            [token_reply(), token_reply(), token_reply('r9Ny' * 8), session_reply()],
            clock=lambda: now[0], delay=.01)
        await service.start()
        try:
            await service.connect(100)
            _, obsolete = await service.connect(200)
            now[0] += 3600
            renewed = asyncio.create_task(service.connect(200))
            await transport.wait_started(3)
            await service.expire_pending()
            _, fresh = await renewed
            with pytest.raises(LastFmError, match='no pending'):
                await service.finish(100)
            with pytest.raises(LastFmError, match='no longer valid'):
                await service.finish(200, obsolete)
            linked = await service.finish(200, fresh)
            assert linked.user_id == '200'
            assert len(transport.requests) == 4
        finally:
            await service.close()
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT user_id FROM pending').fetchall() == []
            assert db.execute('SELECT user_id FROM accounts').fetchall() == [('200',)]
    asyncio.run(run())


def test_approval_requires_explicit_finish_and_relink_fences_old_nonce(tmp_path):
    async def run():
        service, transport = make_service(tmp_path / 'accounts.sqlite3', [token_reply(),
            (200, {'error': 14, 'message': TOKEN}), session_reply(), token_reply('f6' * 16),
            token_reply('07' * 16), session_reply('new-private-name', '18' * 16)])
        await service.start()
        service.scrobbler = SimpleNamespace(forget_user=AsyncMock())
        _, nonce = await service.connect(100)
        assert len(transport.requests) == 1  # no background approval polling
        with pytest.raises(LastFmError) as caught:
            await service.finish(100, nonce)
        assert caught.value.code == 14 and not service.has_accounts
        old = await service.finish(100, nonce)
        _, stale = await service.connect(100)
        assert service.account_for(100) == old
        _, latest = await service.connect(100)
        with pytest.raises(LastFmError, match='no longer valid'):
            await service.finish(100, stale)
        new = await service.finish(100, latest)
        assert new.link_id != old.link_id
        await service.invalidate(100, old.link_id)
        assert service.account_for(100) == new
        await service.invalidate(100, new.link_id)
        assert service.account_for(100) is None
        await service.close()
    asyncio.run(run())


def test_unlink_removes_pending_and_accounts_across_restart(tmp_path):
    async def run():
        path = tmp_path / 'accounts.sqlite3'
        service, _ = make_service(path, [token_reply(), session_reply(), token_reply()])
        await service.start()
        _, nonce = await service.connect(100)
        await service.finish(100, nonce)
        _, nonce = await service.connect(100)
        service.scrobbler = SimpleNamespace(forget_user=AsyncMock())
        await service.disconnect(100)
        assert not service.has_accounts
        await service.close()
        restored, transport = make_service(path, [])
        await restored.start()
        assert restored.account_for(100) is None
        with pytest.raises(LastFmError, match='no pending'):
            await restored.finish(100, nonce)
        assert transport.requests == []
        await restored.close()
    asyncio.run(run())


@pytest.mark.parametrize('tamper', ['wrong-key', 'account-owner', 'pending-owner'])
def test_encryption_fails_closed_on_wrong_key_or_owner_tampering(tmp_path, tamper):
    async def run():
        path = tmp_path / 'accounts.sqlite3'
        service, _ = make_service(path, [token_reply(), session_reply(), token_reply()])
        await service.start()
        _, nonce = await service.connect(100)
        await service.finish(100, nonce)
        await service.connect(100)
        await service.close()
        if tamper.endswith('owner'):
            table = 'accounts' if tamper == 'account-owner' else 'pending'
            with sqlite3.connect(path) as db:
                db.execute(f'UPDATE {table} SET user_id=?', ('200',))
        original = path.read_bytes()
        bad, transport = make_service(path, [], encryption_key='19' * 32 if tamper == 'wrong-key' else ENCRYPTION)
        await bad.start()
        assert not bad.configured and not bad.has_accounts
        assert bad.account_for(100) is None and bad.account_for(200) is None
        with pytest.raises(LastFmError, match='decrypt'):
            await bad.connect(100)
        assert transport.requests == []
        assert path.read_bytes() == original
        await bad.close()
    asyncio.run(run())


def test_missing_configuration_is_actionable_and_creates_no_database(tmp_path):
    async def run():
        path = tmp_path / 'accounts.sqlite3'
        service = LastFmService(path, api_key='', api_secret='', encryption_key='not-a-key')
        await service.start()
        assert not service.configured and not path.exists()
        with pytest.raises(LastFmError) as caught:
            await service.connect(100)
        assert all(name in str(caught.value) for name in ('LASTFM_API_KEY', 'LASTFM_API_SECRET', 'LASTFM_ENCRYPTION_KEY'))
        await service.close()
    asyncio.run(run())


def test_current_requester_tool_never_returns_private_url_or_identity(tmp_path):
    async def run():
        service, _ = make_service(tmp_path / 'accounts.sqlite3', [token_reply(), session_reply()])
        await service.start()
        user = SimpleNamespace(id=100, bot=False, send=AsyncMock())
        message = SimpleNamespace(author=user, guild=SimpleNamespace(id=10))
        tool = LastFmAccountTool(SimpleNamespace(), service)
        connected = await tool.execute(message, 'connect')
        assert json.loads(connected)['private_approval_sent']
        sent = user.send.call_args
        approval = next(button.url for button in sent.kwargs['view'].children if button.url)
        assert parse_qs(urlsplit(approval).query)['token'] == [TOKEN]
        dm = SimpleNamespace(content=sent.args[0], guild=None, components=sent.kwargs['view'].children)
        history_text = render_discord_context_text(dm, include_timestamp=False)
        assert TOKEN not in history_text and KEY not in history_text
        memory = ConversationMemoryManager(str(tmp_path / 'history'))
        try:
            await memory.add_to_channel_memory('90', {
                'content': history_text, 'message_id': '123', 'author_id': '777', 'author_is_bot': True})
        finally:
            await memory.flush()
        assert TOKEN.encode() not in memory.db_path.read_bytes()
        finished = await tool.execute(message, 'finish')
        status = await tool.execute(message, 'status')
        for result in (connected, finished, status):
            assert all(secret not in result for secret in (TOKEN, SESSION, 'private-name', 'https://'))
        result = json.loads(await tool.execute(message, 'disconnect', user_id=200))
        assert not result['succeeded'] and service.account_for(100) is not None
        await service.close()
    asyncio.run(run())


def test_dm_failure_does_not_expose_link_and_dev_guard_blocks_side_effects(tmp_path):
    async def run():
        service, transport = make_service(tmp_path / 'accounts.sqlite3', [token_reply()])
        await service.start()
        error = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), {'message': TOKEN, 'code': 0})
        user = SimpleNamespace(id=100, bot=False, send=AsyncMock(side_effect=error))
        message = SimpleNamespace(author=user, guild=SimpleNamespace(id=10))
        tool = LastFmAccountTool(SimpleNamespace(), service)
        result = await tool.execute(message, 'connect')
        assert '/lastfm connect' in result and TOKEN not in result and 'https://' not in result
        blocked = LastFmAccountTool(SimpleNamespace(_dev_scope_allows=lambda ctx: False), service)
        assert not json.loads(await blocked.execute(message, 'disconnect'))['succeeded']
        assert len(transport.requests) == 1
        await service.close()
    asyncio.run(run())


def test_private_slash_button_is_owner_bound_and_redeems_once(tmp_path):
    async def run():
        service, transport = make_service(tmp_path / 'accounts.sqlite3', [token_reply(), session_reply()])
        await service.start()
        bot = SimpleNamespace()
        install(bot, service)
        handler = next(callback for _, name, callback in user_install._INTERACTION_HANDLERS if name == 'lastfm')
        def interaction(owner, data):
            return SimpleNamespace(user=SimpleNamespace(id=owner, bot=False), guild=SimpleNamespace(id=10), data=data,
                response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        try:
            connect = interaction(100, {'name': 'lastfm', 'options': [{'name': 'connect'}]})
            assert await handler(bot, connect)
            sent = connect.followup.send.call_args
            assert sent.kwargs['ephemeral']
            approval = next(button.url for button in sent.kwargs['view'].children if button.url)
            assert parse_qs(urlsplit(approval).query)['token'] == [TOKEN]
            custom = next(button.custom_id for button in sent.kwargs['view'].children if button.custom_id)
            wrong = interaction(200, {'custom_id': custom})
            await handler(bot, wrong)
            assert 'another requester' in wrong.followup.send.call_args.args[0]
            assert wrong.followup.send.call_args.kwargs['ephemeral'] and len(transport.requests) == 1
            finish = interaction(100, {'custom_id': custom})
            await handler(bot, finish)
            assert service.account_for(100) is not None and service.account_for(200) is None
            assert finish.followup.send.call_args.kwargs['ephemeral']
            await handler(bot, finish)
            assert len(transport.requests) == 2
        finally:
            uninstall()
            await service.close()
    asyncio.run(run())


def test_cancelled_finish_completes_consumed_token_persistence(tmp_path):
    async def run():
        service, transport = make_service(tmp_path / 'accounts.sqlite3',
            [token_reply(), session_reply()], delay=0.04)
        await service.start()
        _, nonce = await service.connect(100)
        finish = asyncio.create_task(service.finish(100, nonce))
        await transport.wait_started(2)
        finish.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finish
        assert service.account_for(100) is not None
        await service.close()
        restored, _ = make_service(tmp_path / 'accounts.sqlite3', [])
        await restored.start()
        assert restored.account_for(100).session_key == SESSION
        with pytest.raises(LastFmError, match='no pending'):
            await restored.finish(100, nonce)
        await restored.close()
    asyncio.run(run())


def test_real_database_work_uses_one_off_loop_executor(tmp_path):
    import threading

    async def run():
        service, _ = make_service(tmp_path / 'accounts.sqlite3', [token_reply(), session_reply()])
        loop_thread = threading.get_ident()
        disk_threads = []
        progress = []
        original_load, original_save = service._load, service._save

        def load():
            disk_threads.append(threading.get_ident())
            time.sleep(0.03)
            return original_load()

        def save(*args, **kwargs):
            disk_threads.append(threading.get_ident())
            time.sleep(0.03)
            return original_save(*args, **kwargs)

        service._load, service._save = load, save

        async def heartbeat():
            for _ in range(20):
                await asyncio.sleep(0.005)
                progress.append(len(disk_threads))

        heartbeat_task = asyncio.create_task(heartbeat())
        await service.start()
        _, nonce = await service.connect(100)
        await service.finish(100, nonce)
        await heartbeat_task
        assert len(disk_threads) == 3 and len(set(disk_threads)) == 1
        assert disk_threads[0] != loop_thread
        assert 1 in progress and 2 in progress
        await service.close()
    asyncio.run(run())


def test_quiesce_drains_consent_cleanup_before_outbox_shutdown(tmp_path):
    async def run():
        service, transport = make_service(tmp_path / 'accounts.sqlite3',
            [token_reply(), session_reply()])
        await service.start()
        _, nonce = await service.connect(100)
        forget_started, forget_allowed = asyncio.Event(), asyncio.Event()

        async def forget(owner):
            assert owner == '100'
            forget_started.set()
            await forget_allowed.wait()

        service.scrobbler = SimpleNamespace(forget_user=forget)
        finish = asyncio.create_task(service.finish(100, nonce))
        await forget_started.wait()
        drain = asyncio.create_task(service.quiesce())
        await asyncio.sleep(0)
        assert not drain.done()
        with pytest.raises(LastFmError, match='shutting down'):
            await service.connect(200)
        assert len(transport.requests) == 2
        forget_allowed.set()
        account = await finish
        await drain
        assert service.account_for(100) == account
        # The manager may still invalidate a revoked session during its own drain.
        await service.invalidate(100, account.link_id)
        assert service.account_for(100) is None
        await service.close()
    asyncio.run(run())
