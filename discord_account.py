"""User-account vs official bot-account Discord transport.

Maxwell can log in with ``DISCORD_TOKEN`` (self-bot user account),
``DISCORD_BOT_TOKEN`` (official bot application), or both. One brain
handles both connections so they act as a single Maxwell:

- overlapping guilds are handled once (the user account wins if it is
  in the room; the official bot covers guilds the user account is not in)
- both Discord user IDs count as "self"
- tools that only a user account can perform (invite-join, onboarding)
  stay off the official bot
- user-account REST is serialized and global 429s queue instead of bursting

discord.py-self speaks the user API. Official bot tokens need a ``Bot ``
Authorization prefix, bot IDENTIFY (intents), and READY without
READY_SUPPLEMENTAL. Those patches are instance-flagged so a user client
in the same process is untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

import discord

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = default
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default
    return max(lo, min(hi, value))


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
    return max(lo, min(hi, value))


# User-account REST is a ban surface. Official bots can burst; self-bots
# cannot. Keep one in-flight call, space the next one, and when Discord
# returns a global 429 park every queued request until retry_after.
_USER_REST_MIN_INTERVAL = _env_float("MAXWELL_USER_REST_MIN_INTERVAL", 0.4, 0.05, 5.0)
_USER_REST_MAX_INFLIGHT = _env_int("MAXWELL_USER_REST_MAX_INFLIGHT", 1, 1, 4)
_USER_REST_429_RETRIES = _env_int("MAXWELL_USER_REST_429_RETRIES", 8, 1, 20)

# Tools that call user-account-only Discord endpoints. Official bot
# accounts cannot accept invites, complete guild onboarding, or sit in
# group DMs. Keep this list the things that 403/fail on a bot token —
# not "stuff we would rather the bot not do".
USER_ONLY_TOOLS = frozenset({"join_server", "server_setup"})

USER_ONLY_UNAVAILABLE = (
    "Error: this action needs the Discord user account (DISCORD_TOKEN). "
    "The official bot account cannot do it. Add the user token, or send "
    "people the bot invite URL (BOT_INVITE_URL) so they can add the bot."
)


class UserRestGate:
    """FIFO throttle for user-account Discord REST.

    Official bot tokens keep discord.py's normal buckets. User tokens share
    one gate: at most a few in-flight calls, a minimum gap between them, and
    a global cooldown that parks the whole queue when Discord says 429.
    """

    def __init__(
        self,
        *,
        min_interval: float | None = None,
        max_inflight: int | None = None,
    ) -> None:
        self.min_interval = (
            _USER_REST_MIN_INTERVAL
            if min_interval is None
            else max(0.0, float(min_interval))
        )
        self.max_inflight = (
            _USER_REST_MAX_INFLIGHT
            if max_inflight is None
            else max(1, int(max_inflight))
        )
        self._sema: asyncio.Semaphore | None = None
        self._admission_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._global_until = 0.0

    def _sema_obj(self) -> asyncio.Semaphore:
        if self._sema is None:
            self._sema = asyncio.Semaphore(self.max_inflight)
        return self._sema

    def note_global(self, retry_after: float) -> None:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = 1.0
        until = time.monotonic() + max(delay, 0.2)
        if until > self._global_until:
            self._global_until = until

    def reset(self) -> None:
        self._sema = None
        self._admission_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._global_until = 0.0

    @contextlib.asynccontextmanager
    async def slot(self):
        sema = self._sema_obj()
        await sema.acquire()
        try:
            async with self._admission_lock:
                while True:
                    now = time.monotonic()
                    delay = max(self._global_until, self._next_slot) - now
                    if delay <= 0:
                        break
                    if self._global_until > now and delay >= 0.2:
                        logger.warning(
                            "User Discord REST queued %.1fs (global rate limit)",
                            delay,
                        )
                    await asyncio.sleep(delay)
                self._next_slot = time.monotonic() + self.min_interval
            yield
        finally:
            sema.release()


_USER_REST = UserRestGate()


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds to park the user REST queue, or None if this is not a 429."""
    name = type(exc).__name__
    text = str(exc).lower()
    if "cloudflare ban" in text or "cloudflare access denied" in text:
        return None
    status = getattr(exc, "status", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status", None)
        if status is None:
            status = getattr(response, "status_code", None)
    if name != "RateLimited" and status != 429:
        return None
    retry = getattr(exc, "retry_after", None)
    if retry is None and response is not None:
        headers = getattr(response, "headers", None) or {}
        getter = getattr(headers, "get", None)
        if callable(getter):
            retry = getter("Retry-After") or getter("retry-after")
    try:
        delay = float(retry) if retry is not None else 1.0
    except (TypeError, ValueError):
        delay = 1.0
    return max(delay, 0.2)


async def user_account_request(http, original, route, *, files=None, form=None, **kwargs):
    """Run one user-account REST call through the global queue."""
    last_error: BaseException | None = None
    for attempt in range(_USER_REST_429_RETRIES):
        async with _USER_REST.slot():
            try:
                return await original(http, route, files=files, form=form, **kwargs)
            except Exception as exc:
                delay = _retry_after_seconds(exc)
                if delay is None:
                    raise
                last_error = exc
                logger.warning(
                    "User Discord REST 429; queueing %.2fs (attempt %s/%s)",
                    delay,
                    attempt + 1,
                    _USER_REST_429_RETRIES,
                )
                _USER_REST.note_global(delay)
                continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("user Discord REST failed")


# Typical privileged + unprivileged intents so MESSAGE_CONTENT works.
# The developer portal must allow the privileged bits or IDENTIFY closes.
_DEFAULT_BOT_INTENTS = 3276799

_PATCHED = False


def user_tools_enabled(planned_kinds: Iterable[str] | None) -> bool:
    """True when a user account is expected this process."""
    kinds = {str(k).strip().lower() for k in (planned_kinds or ())}
    return "user" in kinds


def user_only_unavailable(tool_name: str = "") -> str:
    extra = f" ({tool_name})" if tool_name else ""
    return USER_ONLY_UNAVAILABLE.replace("this action", f"this action{extra}")


def discord_user_client(bot: Any) -> Any | None:
    """The connected user-account client, or None.

    Test doubles without ``account_kind`` are treated as the user account
    so existing FakeBot fixtures keep working.
    """
    if bot is None:
        return None
    if not hasattr(bot, "account_kind"):
        return bot
    if getattr(bot, "account_kind", "user") == "user":
        return bot
    peer = getattr(bot, "_peer", None)
    if peer is not None and getattr(peer, "account_kind", None) == "user":
        return peer
    return None


def discord_bot_client(bot: Any) -> Any | None:
    if bot is None:
        return None
    if getattr(bot, "account_kind", None) == "bot":
        return bot
    peer = getattr(bot, "_peer", None)
    if peer is not None and getattr(peer, "account_kind", None) == "bot":
        return peer
    return None


def account_ids(bot: Any) -> set[int]:
    """Discord snowflakes that are Maxwell on either connection."""
    ids: set[int] = set()
    stored = getattr(bot, "_account_ids", None)
    if stored:
        ids.update(int(x) for x in stored if x is not None)
    for client in (bot, getattr(bot, "_peer", None)):
        user = getattr(client, "user", None) if client is not None else None
        uid = getattr(user, "id", None)
        if uid is not None:
            with contextlib.suppress(TypeError, ValueError):
                ids.add(int(uid))
    return ids


def native_get_channel(client: Any, channel_id: int):
    """Channel lookup on this client only — not the dual-account overlay."""
    for cls in type(client).mro():
        if cls.__name__ == "Client" and str(getattr(cls, "__module__", "")).startswith(
            "discord"
        ):
            getter = getattr(cls, "get_channel", None)
            if callable(getter):
                return getter(client, channel_id)
    getter = getattr(client, "get_channel", None)
    if callable(getter):
        return getter(channel_id)
    return None


def native_get_guild(client: Any, guild_id: int):
    for cls in type(client).mro():
        if cls.__name__ == "Client" and str(getattr(cls, "__module__", "")).startswith(
            "discord"
        ):
            getter = getattr(cls, "get_guild", None)
            if callable(getter):
                return getter(client, guild_id)
    getter = getattr(client, "get_guild", None)
    if callable(getter):
        return getter(guild_id)
    return None


def client_sees_channel(client: Any, channel_id: int) -> bool:
    if client is None or not channel_id:
        return False
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return False
    return native_get_channel(client, cid) is not None


def client_for_channel(bot: Any, channel_id: Any = None, message: Any = None) -> Any:
    """Client that can see this channel. Prefer the user account when both can."""
    cid = channel_id
    if cid is None and message is not None:
        cid = getattr(getattr(message, "channel", None), "id", None)
    try:
        cid_int = int(cid) if cid is not None else None
    except (TypeError, ValueError):
        cid_int = None
    user = discord_user_client(bot)
    official = discord_bot_client(bot)
    if cid_int is not None:
        if client_sees_channel(user, cid_int):
            return user
        if client_sees_channel(official, cid_int):
            return official
        # The message object is bound to the client that received it.
        if message is not None:
            state = getattr(message, "_state", None)
            owner = getattr(state, "_client", None) or getattr(state, "client", None)
            if owner is not None:
                return owner
    return user or official or bot


def _intents_all():
    intents_cls = getattr(discord, "Intents", None)
    if intents_cls is None:
        return None
    with contextlib.suppress(Exception):
        return intents_cls.all()
    with contextlib.suppress(Exception):
        return intents_cls.default()
    return None


def _bot_intents_value() -> int:
    intents = _intents_all()
    value = getattr(intents, "value", None)
    if isinstance(value, int) and value > 0:
        return value
    return _DEFAULT_BOT_INTENTS


def _client_kwargs(*, bot_account: bool, captcha_handler=None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    intents = _intents_all()
    if intents is not None:
        kwargs["intents"] = intents
    if bot_account:
        kwargs["guild_subscriptions"] = False
        kwargs["chunk_guilds_at_startup"] = False
    else:
        kwargs["mobile_status"] = True
        if captcha_handler is not None:
            kwargs["captcha_handler"] = captcha_handler
    return kwargs


def _make_client(kwargs: dict[str, Any]) -> discord.Client:
    """Construct a Client, dropping kwargs this discord.py-self build rejects."""
    pending = dict(kwargs)
    last_error: TypeError | None = None
    for _ in range(len(pending) + 1):
        try:
            return discord.Client(**pending)
        except TypeError as exc:
            last_error = exc
            dropped = False
            for key in list(pending):
                if key in str(exc):
                    pending.pop(key, None)
                    dropped = True
                    break
            if not dropped:
                # Unknown which kwarg; peel optional ones.
                for key in (
                    "guild_subscriptions",
                    "chunk_guilds_at_startup",
                    "mobile_status",
                    "captcha_handler",
                    "intents",
                ):
                    if key in pending:
                        pending.pop(key)
                        dropped = True
                        break
            if not dropped:
                raise
    if last_error is not None:
        raise last_error
    return discord.Client()


def _ensure_bot_http_token(http: Any) -> None:
    """discord.py-self stores the raw token and sends it as Authorization.

    Official bot accounts need ``Bot <token>`` on HTTP, but IDENTIFY still
    wants the raw token. Keep both on the HTTP client.
    """
    if http is None or not getattr(http, "_bot_account", False):
        return
    current = getattr(http, "token", None)
    if not isinstance(current, str) or not current.strip():
        return
    raw = _strip_bot_prefix(current)
    http._raw_token = raw
    http.token = f"Bot {raw}"


_HTTP_PATCH_VERSION = 3


def install_library_patches() -> None:
    """Idempotent class-level patches for bot HTTP and user REST queuing."""
    global _PATCHED
    from discord.http import HTTPClient

    if int(getattr(HTTPClient, "_maxwell_bot_http_version", 0) or 0) >= _HTTP_PATCH_VERSION:
        _PATCHED = True
        return
    _patch_http_token()
    _patch_http_static_login()
    _patch_http_request_auth()
    _patch_http_close()
    _patch_http_gateway()
    _patch_identify()
    _patch_ready()
    HTTPClient._maxwell_bot_http = True
    HTTPClient._maxwell_bot_http_version = _HTTP_PATCH_VERSION
    _PATCHED = True


def apply_bot_account_patches(client: Any) -> None:
    """Mark this client as an official bot account before ``login()``."""
    install_library_patches()
    http = getattr(client, "http", None)
    if http is not None:
        http._bot_account = True
        raw = getattr(http, "token", None)
        if isinstance(raw, str) and raw.strip() and not raw.lower().startswith("bot "):
            http._raw_token = raw.strip()
            http.token = f"Bot {raw.strip()}"


def _strip_bot_prefix(token: str) -> str:
    text = (token or "").strip()
    if text.lower().startswith("bot "):
        return text[4:].strip()
    return text


def _patch_http_token() -> None:
    from discord.http import HTTPClient

    original = HTTPClient._token

    def _token(self, token: str) -> None:  # type: ignore[no-untyped-def]
        raw = _strip_bot_prefix(token)
        self._raw_token = raw
        if getattr(self, "_bot_account", False):
            original(self, f"Bot {raw}")
        else:
            original(self, raw)

    HTTPClient._token = _token  # type: ignore[method-assign]


def _patch_http_static_login() -> None:
    """static_login assigns ``self.token = token`` and never calls ``_token``."""
    from discord.http import HTTPClient

    original = HTTPClient.static_login

    async def static_login(self, token: str):  # type: ignore[no-untyped-def]
        if getattr(self, "_bot_account", False):
            raw = _strip_bot_prefix(token)
            self._raw_token = raw
            token = f"Bot {raw}"
        return await original(self, token)

    HTTPClient.static_login = static_login  # type: ignore[method-assign]


BOT_USER_AGENT = "DiscordBot (https://github.com/Z3ki/Maxwell-bot, 1.0)"
_DISCORD_API = "https://discord.com/api/v10"


async def _ensure_aiohttp(http: Any):
    import aiohttp

    session = getattr(http, "_maxwell_aiohttp", None)
    if session is None or getattr(session, "closed", False):
        session = aiohttp.ClientSession()
        http._maxwell_aiohttp = session
    return session


async def _aiohttp_bot_request(http: Any, route: Any, *, files=None, form=None, **kwargs):
    """REST for official bot tokens: aiohttp, no Chrome impersonation.

    discord.py-self's curl_cffi browser fingerprint + ``Bot`` auth is what
    Cloudflare 1020s (error 40333) on channel/message routes.
    """
    import aiohttp
    from discord.errors import DiscordServerError, Forbidden, HTTPException, NotFound

    _ensure_bot_http_token(http)
    method = str(getattr(route, "method", "GET") or "GET").upper()
    url = str(getattr(route, "url", "") or "")
    session = await _ensure_aiohttp(http)
    headers = {
        "Authorization": http.token,
        "User-Agent": BOT_USER_AGENT,
        "Accept": "application/json",
    }
    if kwargs.pop("auth", True) is False:
        headers.pop("Authorization", None)
    reason = kwargs.pop("reason", None)
    if reason:
        headers["X-Audit-Log-Reason"] = str(reason)
    kwargs.pop("context_properties", None)
    extra = kwargs.pop("headers", None)
    if isinstance(extra, dict):
        # Chrome client-hint dumps from discord.py-self must not ride along.
        for key, value in extra.items():
            kl = str(key).lower()
            if kl.startswith("sec-") or kl in {
                "origin",
                "referer",
                "x-super-properties",
                "x-context-properties",
                "x-debug-options",
            }:
                continue
            headers[key] = value

    json_payload = kwargs.pop("json", None)
    data = kwargs.pop("data", None)
    params = kwargs.pop("params", None)
    kwargs.pop("proxy", None)
    kwargs.pop("proxy_auth", None)
    kwargs.pop("interface", None)

    last_error: Exception | None = None
    for attempt in range(5):
        try:
            formdata = None
            if form or files:
                formdata = aiohttp.FormData()
                for file_obj in files or []:
                    reset = getattr(file_obj, "reset", None)
                    if callable(reset):
                        reset(seek=attempt)
                names = {part.get("name") for part in form or []}
                if "payload_json" not in names:
                    if json_payload is not None:
                        import json as _json

                        formdata.add_field("payload_json", _json.dumps(json_payload))
                    elif data is not None and not isinstance(data, (bytes, bytearray)):
                        formdata.add_field("payload_json", data)
                for part in form or []:
                    formdata.add_field(
                        part.get("name") or "file",
                        part.get("data"),
                        filename=part.get("filename"),
                        content_type=part.get("content_type"),
                    )
                # discord.py supplies file streams in form and keeps files for
                # rewinding. Do not upload those attachments a second time.
                for idx, file_obj in enumerate(files or []):
                    field_name = f"files[{idx}]"
                    if field_name not in names:
                        formdata.add_field(
                            field_name,
                            getattr(file_obj, "fp", file_obj),
                            filename=getattr(file_obj, "filename", None) or f"file{idx}",
                        )
            body = formdata if formdata is not None else data
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_payload if formdata is None else None,
                data=body,
                params=params,
            ) as resp:
                ctype = (resp.content_type or "").lower()
                if resp.status == 204:
                    payload = None
                elif "json" in ctype:
                    payload = await resp.json(content_type=None)
                else:
                    text = await resp.text()
                    payload = text
                if 200 <= resp.status < 300:
                    return payload
                if resp.status == 429:
                    retry = resp.headers.get("Retry-After") or "1"
                    try:
                        delay = float(retry)
                    except (TypeError, ValueError):
                        delay = 1.0
                    if isinstance(payload, dict) and payload.get("retry_after") is not None:
                        with contextlib.suppress(TypeError, ValueError):
                            delay = float(payload["retry_after"])
                    await asyncio.sleep(max(delay, 0.2))
                    continue
                if resp.status in {502, 504, 507, 522, 523, 524}:
                    await asyncio.sleep(1 + attempt * 2)
                    continue
                if resp.status == 403:
                    raise Forbidden(resp, payload)
                if resp.status == 404:
                    raise NotFound(resp, payload)
                if resp.status >= 500:
                    raise DiscordServerError(resp, payload)
                raise HTTPException(resp, payload)
        except (Forbidden, NotFound, HTTPException, DiscordServerError):
            raise
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(1 + attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("Discord bot HTTP request failed")


def _patch_http_request_auth() -> None:
    """Bot REST goes through aiohttp; user accounts keep curl_cffi, queued."""
    from discord.http import HTTPClient

    original = HTTPClient.request

    async def request(self, route, *args, files=None, form=None, **kwargs):  # type: ignore[no-untyped-def]
        _ensure_bot_http_token(self)
        if getattr(self, "_bot_account", False):
            if args:
                # discord.py-self only ever uses keyword files/form.
                kwargs.setdefault("files", files)
                kwargs.setdefault("form", form)
                return await _aiohttp_bot_request(self, route, **kwargs)
            return await _aiohttp_bot_request(self, route, files=files, form=form, **kwargs)
        return await user_account_request(
            self, original, route, files=files, form=form, **kwargs
        )

    HTTPClient.request = request  # type: ignore[method-assign]


def _patch_http_close() -> None:
    from discord.http import HTTPClient

    original = HTTPClient.close

    async def close(self):  # type: ignore[no-untyped-def]
        session = getattr(self, "_maxwell_aiohttp", None)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
            self._maxwell_aiohttp = None
        return await original(self)

    HTTPClient.close = close  # type: ignore[method-assign]


async def clear_application_commands(
    token: str,
    *,
    application_id: str | int | None = None,
    guild_ids: Iterable[int] | None = None,
) -> dict[str, int]:
    """Overwrite registered slash/app commands with an empty list.

    Leftover commands (from OpenClaw or another bot on the same application)
    stay on Discord until something PUTs a new set. Maxwell does not use
    slash commands, so startup wipes them.
    """
    import aiohttp

    token = _strip_bot_prefix(token)
    removed = {"global": 0, "guild": 0}
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": BOT_USER_AGENT,
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        app_id = str(application_id or "").strip()
        if not app_id:
            async with session.get(f"{_DISCORD_API}/oauth2/applications/@me") as resp:
                data = await resp.json() if resp.status == 200 else {}
                app_id = str((data or {}).get("id") or "")
        if not app_id:
            logger.warning("Could not resolve application id; slash commands not cleared")
            return removed
        async with session.get(f"{_DISCORD_API}/applications/{app_id}/commands") as resp:
            current = await resp.json() if resp.status == 200 else []
        removed["global"] = len(current) if isinstance(current, list) else 0
        async with session.put(
            f"{_DISCORD_API}/applications/{app_id}/commands", json=[]
        ) as resp:
            if resp.status not in {200, 201}:
                body = await resp.text()
                logger.warning(
                    "Failed to clear global slash commands: HTTP %s %s",
                    resp.status,
                    body[:200],
                )
            else:
                logger.info("Cleared %s global slash command(s)", removed["global"])
        for gid in guild_ids or ():
            async with session.get(
                f"{_DISCORD_API}/applications/{app_id}/guilds/{gid}/commands"
            ) as resp:
                current = await resp.json() if resp.status == 200 else []
            n = len(current) if isinstance(current, list) else 0
            async with session.put(
                f"{_DISCORD_API}/applications/{app_id}/guilds/{gid}/commands",
                json=[],
            ) as resp:
                if resp.status in {200, 201}:
                    removed["guild"] += n
    return removed


def _patch_http_gateway() -> None:
    from discord.errors import HTTPException
    from discord.http import HTTPClient, Route

    original = HTTPClient.get_gateway

    async def get_gateway(self, *, encoding: str = "json", compress=None):  # type: ignore[no-untyped-def]
        if not getattr(self, "_bot_account", False):
            return await original(self, encoding=encoding, compress=compress)
        try:
            data = await self.request(Route("GET", "/gateway/bot"))
        except HTTPException:
            data = await self.request(Route("GET", "/gateway"))
        url = data["url"]
        if compress:
            return f"{url}?encoding={encoding}&v=9&compress={compress}"
        return f"{url}?encoding={encoding}&v=9"

    HTTPClient.get_gateway = get_gateway  # type: ignore[method-assign]


def _patch_identify() -> None:
    from discord.gateway import DiscordWebSocket

    original = DiscordWebSocket.identify

    async def identify(self):  # type: ignore[no-untyped-def]
        http = getattr(getattr(self, "_connection", None), "http", None)
        if not getattr(http, "_bot_account", False):
            return await original(self)
        raw = getattr(http, "_raw_token", None)
        if not raw:
            token = getattr(http, "token", "") or ""
            raw = _strip_bot_prefix(token)
        payload = {
            "op": self.IDENTIFY,
            "d": {
                "token": raw,
                "intents": _bot_intents_value(),
                "properties": {
                    "os": sys.platform,
                    "browser": "maxwell",
                    "device": "maxwell",
                },
                "compress": True,
                "large_threshold": 250,
            },
        }
        await self.call_hooks("before_identify", initial=self._initial_identify)
        await self.send_as_json(payload)
        logger.debug("Gateway has sent the bot IDENTIFY payload.")
        self._initial_identify = True

    DiscordWebSocket.identify = identify  # type: ignore[method-assign]


def _patch_ready() -> None:
    from discord.state import ConnectionState
    from discord.user import ClientUser

    original = ConnectionState.parse_ready

    def parse_ready(self, data):  # type: ignore[no-untyped-def]
        http = getattr(self, "http", None)
        if not getattr(http, "_bot_account", False):
            return original(self, data)
        # Official bots never send READY_SUPPLEMENTAL. Finish READY here
        # the way upstream discord.py does, then run _delay_ready.
        if self._ready_task is not None:
            self._ready_task.cancel()
        self.clear()
        self._ready_data = None
        if http is not None:
            http.ack_token = None
        user = ClientUser(state=self, data=data["user"])
        self.user = user
        self._users[user.id] = user  # type: ignore[index]
        for guild_data in data.get("guilds", []) or []:
            with contextlib.suppress(Exception):
                self._add_guild_from_data(guild_data)
        self.call_handlers("connect")
        self.dispatch("connect")
        self._ready_task = asyncio.create_task(self._delay_ready())

    ConnectionState.parse_ready = parse_ready  # type: ignore[method-assign]


@dataclass
class AccountLogin:
    kind: str  # "user" | "bot"
    token: str
    label: str = ""


async def _login_probe(token: str, *, bot_account: bool) -> tuple[bool, str]:
    """Login-only probe using the same HTTP stack Maxwell will use."""
    token = _strip_bot_prefix(token)
    if not token:
        return False, "empty token"
    install_library_patches()
    kwargs = _client_kwargs(bot_account=bot_account)
    client = _make_client(kwargs)
    if bot_account:
        apply_bot_account_patches(client)
    try:
        await client.login(token)
        user = client.user
        name = getattr(user, "name", None) or "?"
        uid = getattr(user, "id", None) or "?"
        is_bot = bool(getattr(user, "bot", False))
        await client.close()
        if bot_account and not is_bot:
            return False, "token belongs to a user account, not a bot"
        if (not bot_account) and is_bot:
            return False, "token belongs to a bot; set DISCORD_BOT_TOKEN"
        return True, f"{name} ({uid})"
    except discord.LoginFailure:
        with contextlib.suppress(Exception):
            await client.close()
        return False, "rejected (401)"
    except Exception as exc:
        with contextlib.suppress(Exception):
            await client.close()
        return False, f"{type(exc).__name__}: {exc}"


async def resolve_accounts(
    user_token: str | None,
    bot_token: str | None,
    mode: str = "auto",
) -> list[AccountLogin]:
    """Probe configured tokens. Returns the accounts that actually work.

    ``mode``: auto (default) | user | bot.
    A bot token stuffed into ``DISCORD_TOKEN`` is detected and treated as bot.
    """
    mode = (mode or "auto").strip().lower()
    if mode not in {"auto", "user", "bot"}:
        mode = "auto"
    user_token = (user_token or "").strip()
    bot_token = (bot_token or "").strip()
    if user_token.lower().startswith("bot "):
        # Someone pasted "Bot xyz" into the user-token field.
        maybe = _strip_bot_prefix(user_token)
        if not bot_token:
            bot_token = maybe
        user_token = ""

    want_user = bool(user_token) and mode in {"auto", "user"}
    want_bot = bool(bot_token) and mode in {"auto", "bot"}
    found: list[AccountLogin] = []

    if want_user:
        ok, label = await _login_probe(user_token, bot_account=False)
        if ok:
            found.append(AccountLogin(kind="user", token=user_token, label=label))
            logger.info("Discord user account accepted: %s", label)
        else:
            ok_bot, bot_label = await _login_probe(user_token, bot_account=True)
            if ok_bot:
                logger.warning(
                    "DISCORD_TOKEN is an official bot token (%s). "
                    "Running in bot-account mode; user-only tools are off. "
                    "Put this value in DISCORD_BOT_TOKEN and keep DISCORD_TOKEN "
                    "as the user token if you want both.",
                    bot_label,
                )
                found.append(AccountLogin(kind="bot", token=user_token, label=bot_label))
                want_bot = False
            else:
                logger.error(
                    "DISCORD_TOKEN rejected (%s). "
                    "Set a fresh user token, or set DISCORD_BOT_TOKEN to fall back "
                    "to an official bot account.",
                    label,
                )

    if want_bot:
        # Skip a second probe if we already accepted this exact token as bot.
        if any(a.kind == "bot" and a.token == bot_token for a in found):
            return found
        ok, label = await _login_probe(bot_token, bot_account=True)
        if ok:
            found.append(AccountLogin(kind="bot", token=bot_token, label=label))
            logger.info("Discord bot account accepted: %s", label)
        else:
            logger.error("DISCORD_BOT_TOKEN rejected (%s).", label)

    return found


def pick_primary(accounts: list[AccountLogin]) -> tuple[AccountLogin, AccountLogin | None]:
    """User account is primary when present so user-only tools keep working."""
    if not accounts:
        raise RuntimeError("no Discord accounts")
    user = next((a for a in accounts if a.kind == "user"), None)
    bot = next((a for a in accounts if a.kind == "bot"), None)
    if user is not None:
        return user, bot
    return accounts[0], None


class CompanionClient(discord.Client):
    """Second Discord connection that feeds the primary MaxwellBot brain."""

    def __init__(self, primary: Any, kind: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.primary = primary
        self.account_kind = kind

    async def on_ready(self):
        primary = self.primary
        primary._peer = self
        user = self.user
        if user is not None:
            ids = getattr(primary, "_account_ids", None)
            if ids is None:
                primary._account_ids = set()
                ids = primary._account_ids
            ids.add(int(user.id))
        logger.info(
            "Companion %s account logged in as %s (%s) — %s guilds",
            self.account_kind,
            getattr(user, "name", "?"),
            getattr(user, "id", "?"),
            len(self.guilds),
        )

    async def on_message(self, message):
        primary = self.primary
        if primary is None:
            return
        author_id = getattr(getattr(message, "author", None), "id", None)
        if author_id is not None and int(author_id) in account_ids(primary):
            return
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        # Shared guild: the primary (usually the user account) handles it.
        if channel_id is not None and client_sees_channel(primary, int(channel_id)):
            return
        await primary.on_message(message)

    async def on_message_edit(self, before, after):
        primary = self.primary
        handler = getattr(primary, "on_message_edit", None)
        if not callable(handler):
            return
        channel_id = getattr(getattr(after, "channel", None), "id", None)
        if channel_id is not None and client_sees_channel(primary, int(channel_id)):
            return
        await handler(before, after)

    async def on_disconnect(self):
        logger.warning("Companion %s Discord gateway disconnected", self.account_kind)


def make_companion(primary: Any, kind: str) -> CompanionClient:
    bot_account = kind == "bot"
    kwargs = _client_kwargs(
        bot_account=bot_account,
        captcha_handler=getattr(primary, "_handle_captcha", None)
        if not bot_account
        else None,
    )
    pending = dict(kwargs)
    last_error: TypeError | None = None
    client: CompanionClient | None = None
    for _ in range(len(pending) + 1):
        try:
            client = CompanionClient(primary, kind, **pending)
            break
        except TypeError as exc:
            last_error = exc
            dropped = False
            for key in list(pending):
                if key in str(exc):
                    pending.pop(key, None)
                    dropped = True
                    break
            if not dropped:
                raise
    if client is None:
        if last_error is not None:
            raise last_error
        client = CompanionClient(primary, kind)
    if bot_account:
        apply_bot_account_patches(client)
    return client
