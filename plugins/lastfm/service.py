"""Owner-bound encrypted Last.fm consent, with serial off-loop SQLite work."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from urllib.parse import urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .client import LastFmClient, LastFmError, valid_api_credentials


@dataclass(frozen=True)
class LinkedAccount:
    user_id: str
    username: str
    session_key: str = field(repr=False)
    link_id: str


@dataclass(frozen=True)
class _Pending:
    token: str = field(repr=False)
    nonce: str = field(repr=False)
    expires: float


class LastFmService:
    def __init__(self, store_path, *, client=None, api_key=None, api_secret=None,
                 encryption_key=None, clock=time.time):
        self.path = Path(store_path)
        self._api_key = os.getenv('LASTFM_API_KEY', '') if api_key is None else api_key
        secret = os.getenv('LASTFM_API_SECRET', '') if api_secret is None else api_secret
        encryption = os.getenv('LASTFM_ENCRYPTION_KEY', '') if encryption_key is None else encryption_key
        self._key = (bytes.fromhex(encryption) if isinstance(encryption, str)
                     and re.fullmatch(r'[0-9a-fA-F]{64}', encryption) else None)
        self.configured = valid_api_credentials(self._api_key, secret) and self._key is not None
        self.configuration_message = (
            'Last.fm is unavailable. Ask the operator to configure valid LASTFM_API_KEY and '
            'LASTFM_API_SECRET (Last.fm application credentials), plus LASTFM_ENCRYPTION_KEY '
            '(an independent 64-hex-character encryption key), then reload the plugin.')
        self.client = client if client is not None else LastFmClient(self._api_key, secret)
        self.scrobbler = None
        self._clock = clock
        self._accounts: dict[str, LinkedAccount] = {}
        self._pending: dict[str, _Pending] = {}
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lastfm-accounts')
        self._started = False
        self._closed = False
        self._accepting = True
        self._operations: set[asyncio.Task] = set()

    @staticmethod
    def _owner(user_id):
        owner = str(user_id or '')
        if not owner.isascii() or not owner.isdigit() or int(owner) <= 0:
            raise LastFmError('An authenticated Discord requester is required.')
        return str(int(owner))

    @property
    def has_accounts(self):
        return bool(self._accounts)

    def account_for(self, user_id):
        return self._accounts.get(self._owner(user_id)) if self.configured and not self._closed else None

    def _require(self):
        if not self.configured:
            raise LastFmError(self.configuration_message)
        if not self._started or self._closed:
            raise LastFmError('Last.fm is not available. Try again after the plugin is ready.')

    def _admit(self):
        self._require()
        if not self._accepting:
            raise LastFmError('Last.fm is shutting down. Try again after the plugin reloads.')

    @asynccontextmanager
    async def _lock(self, owner):
        # Count holders AND queued operations, so idle owners can be forgotten
        # without splitting concurrent consent changes across different locks.
        lock, users = self._locks.get(owner, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[owner] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            users = self._locks[owner][1] - 1
            if users:
                self._locks[owner] = (lock, users)
            else:
                self._locks.pop(owner)

    def _encode(self, owner, purpose, payload):
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, json.dumps(payload).encode(),
                                               f'maxwell-lastfm-v1\0{owner}\0{purpose}'.encode())
        return nonce, ciphertext

    def _decode(self, owner, purpose, nonce, ciphertext):
        try:
            return json.loads(AESGCM(self._key).decrypt(bytes(nonce), bytes(ciphertext),
                f'maxwell-lastfm-v1\0{owner}\0{purpose}'.encode()))
        except (InvalidTag, ValueError, UnicodeError, TypeError):
            raise LastFmError('Stored Last.fm credentials could not be decrypted. '
                'Ask the operator to restore the original LASTFM_ENCRYPTION_KEY; no accounts were loaded.') from None

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA journal_mode=DELETE')
        return db

    def _load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        db = self._db()
        try:
            for table in ('accounts', 'pending'):
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} '
                           '(user_id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL)')
            db.commit()
            accounts, pending = {}, {}
            for owner, nonce, ciphertext in db.execute('SELECT user_id,nonce,ciphertext FROM accounts'):
                payload = self._decode(owner, 'account', nonce, ciphertext)
                account = LinkedAccount(owner, payload['username'], payload['session_key'], payload['link_id'])
                if not all(isinstance(v, str) and v for v in (account.username, account.session_key, account.link_id)):
                    raise LastFmError('Stored Last.fm account data is invalid. Ask the operator to restore the account database.')
                accounts[owner] = account
            for owner, nonce, ciphertext in db.execute('SELECT user_id,nonce,ciphertext FROM pending'):
                payload = self._decode(owner, 'pending', nonce, ciphertext)
                approval = _Pending(payload['token'], payload['nonce'], float(payload['expires']))
                if approval.expires > self._clock():
                    pending[owner] = approval
                else:
                    db.execute('DELETE FROM pending WHERE user_id=?', (owner,))
            db.commit()
            return accounts, pending
        finally:
            db.close()

    def _save(self, owner, *, account=None, pending=None, remove_account=False, remove_pending=False):
        db = self._db()
        try:
            with db:
                if remove_account:
                    db.execute('DELETE FROM accounts WHERE user_id=?', (owner,))
                if remove_pending:
                    db.execute('DELETE FROM pending WHERE user_id=?', (owner,))
                if account is not None:
                    nonce, ciphertext = self._encode(owner, 'account', {
                        'username': account.username, 'session_key': account.session_key, 'link_id': account.link_id})
                    db.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?)', (owner, nonce, ciphertext))
                if pending is not None:
                    nonce, ciphertext = self._encode(owner, 'pending', {
                        'token': pending.token, 'nonce': pending.nonce, 'expires': pending.expires})
                    db.execute('INSERT OR REPLACE INTO pending VALUES (?,?,?)', (owner, nonce, ciphertext))
        finally:
            db.close()

    async def _disk(self, fn, *args, **kwargs):
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self._executor, lambda: fn(*args, **kwargs))
        except LastFmError:
            raise
        except Exception:
            raise LastFmError('Last.fm account storage is unavailable. Ask the operator to check the private account database.') from None

    async def _complete(self, operation):
        # Cancellation cannot interrupt a consumed token between redemption and persistence,
        # or release an owner lock while its disk write is still running.
        task = asyncio.create_task(operation)
        self._operations.add(task)
        task.add_done_callback(self._operations.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def start(self):
        if self._started:
            return
        if self.configured:
            try:
                self._accounts, self._pending = await self._disk(self._load)
            except LastFmError as exc:
                self.configured = False
                self.configuration_message = str(exc)
        self._started = True

    async def connect(self, user_id):
        self._admit()
        return await self._complete(self._connect(self._owner(user_id)))

    async def _connect(self, owner):
        async with self._lock(owner):
            self._require()
            # Token lifetime starts at issue time, never after persistence/network delay.
            issued_at = self._clock()
            token = await self.client.get_token()
            pending = _Pending(token, secrets.token_urlsafe(24), issued_at + 3600)
            await self._disk(self._save, owner, pending=pending)
            self._pending[owner] = pending
            return 'https://www.last.fm/api/auth/?' + urlencode({'api_key': self._api_key, 'token': token}), pending.nonce

    async def finish(self, user_id, nonce=None):
        self._admit()
        return await self._complete(self._finish(self._owner(user_id), nonce))

    async def _finish(self, owner, nonce):
        async with self._lock(owner):
            self._require()
            pending = self._pending.get(owner)
            if pending is None:
                raise LastFmError('There is no pending approval. Start /lastfm connect first.')
            if nonce is not None and (not isinstance(nonce, str) or not secrets.compare_digest(nonce, pending.nonce)):
                raise LastFmError('This linking button is no longer valid. Use your latest /lastfm connect reply.')
            if self._clock() >= pending.expires:
                await self._disk(self._save, owner, remove_pending=True)
                self._pending.pop(owner, None)
                raise LastFmError('This Last.fm approval has expired. Start /lastfm connect again.', code=15)
            try:
                username, session_key = await self.client.get_session(pending.token)
            except LastFmError as exc:
                if exc.code in {4, 15}:
                    await self._disk(self._save, owner, remove_pending=True)
                    self._pending.pop(owner, None)
                raise
            account = LinkedAccount(owner, username, session_key, secrets.token_hex(16))
            await self._disk(self._save, owner, account=account, remove_pending=True)
            self._accounts[owner] = account
            self._pending.pop(owner, None)
            if self.scrobbler is not None:
                await self.scrobbler.forget_user(owner)
            return account

    async def expire_pending(self):
        """Bound scratch consent state without deleting a concurrently renewed token."""
        if not self.configured or not self._accepting or self._closed:
            return
        now = self._clock()
        owners = tuple(islice((owner for owner, pending in self._pending.items() if pending.expires <= now), 64))
        for owner in owners:
            if not self._accepting:
                return
            await self._complete(self._expire_pending(owner))

    async def _expire_pending(self, owner):
        async with self._lock(owner):
            pending = self._pending.get(owner)
            if pending is not None and pending.expires <= self._clock():
                await self._disk(self._save, owner, remove_pending=True)
                self._pending.pop(owner, None)

    async def disconnect(self, user_id):
        self._admit()
        await self._complete(self._disconnect(self._owner(user_id)))

    async def _disconnect(self, owner):
        async with self._lock(owner):
            self._require()
            await self._disk(self._save, owner, remove_account=True, remove_pending=True)
            self._accounts.pop(owner, None)
            self._pending.pop(owner, None)
            if self.scrobbler is not None:
                await self.scrobbler.forget_user(owner)

    async def invalidate(self, user_id, link_id):
        if not self.configured or self._closed:
            return
        await self._complete(self._invalidate(self._owner(user_id), link_id))

    async def _invalidate(self, owner, link_id):
        async with self._lock(owner):
            account = self._accounts.get(owner)
            if account is None or account.link_id != link_id:
                return
            await self._disk(self._save, owner, remove_account=True)
            self._accounts.pop(owner, None)
            if self.scrobbler is not None:
                await self.scrobbler.forget_user(owner)

    async def quiesce(self):
        """Drain admitted consent changes before the scrobble outbox closes."""
        self._accepting = False
        while self._operations:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._operations)),
                                 return_exceptions=True)

    async def close(self):
        await self.quiesce()
        self._closed = True
        await self.client.close()
        await asyncio.get_running_loop().run_in_executor(None, self._executor.shutdown, True)
        self._accounts.clear()
        self._pending.clear()
