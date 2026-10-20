"""Public /u/<handle>/ pages and signed /u/_s/ downloads out of user storage.

Everything served here was written by a user (or by Maxwell on a user's
behalf), so it is hostile until proven otherwise. It shares a host with the
dashboard and the operator API, which is why every response carries a CSP
``sandbox`` without allow-same-origin: the page gets an opaque origin, no
cookies, no storage, and no same-origin reads of anything else on this host.
Games still run; they just can't touch anyone's session.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from site_backend import RateLimiter

logger = logging.getLogger(__name__)

SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals "
    "allow-pointer-lock allow-downloads; frame-ancestors 'none'"
)

# Sandboxed pages throw SecurityError on localStorage/sessionStorage/cookie
# access, and roughly every LLM-written game reads a high score on line one.
# Swap in an in-memory stand-in so they boot instead of dying on a blank
# canvas. Nothing persists, which is the point of the sandbox.
_SHIM = (
    b"<script>(function(){function m(){var d=Object.create(null);return{get length(){"
    b"return Object.keys(d).length},key:function(i){var k=Object.keys(d);return i<k.length?k[i]:null},"
    b"getItem:function(k){k=String(k);return k in d?d[k]:null},setItem:function(k,v){d[String(k)]=String(v)},"
    b"removeItem:function(k){delete d[String(k)]},clear:function(){d=Object.create(null)}}}"
    b"['localStorage','sessionStorage'].forEach(function(n){try{window[n].length}catch(e){try{"
    b"Object.defineProperty(window,n,{value:m(),configurable:true})}catch(_){}}});"
    b"try{document.cookie}catch(e){try{Object.defineProperty(document,'cookie',{get:function(){return''},"
    b"set:function(){},configurable:true})}catch(_){}}})();</script>"
)
_HEAD_RE = re.compile(rb"<head\b[^>]*>", re.I)
_HTML_RE = re.compile(rb"<html\b[^>]*>", re.I)
_DOCTYPE_RE = re.compile(rb"<!doctype[^>]*>", re.I)
_SHIM_LIMIT = 8 * 1024 * 1024
_INLINE_PREFIXES = ("image/", "audio/", "video/", "text/")
_INLINE_TYPES = {"application/pdf", "application/json", "application/javascript"}
_TEXT_TYPES = {"application/json", "application/javascript", "image/svg+xml", "application/xml"}

mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/gltf+json", ".gltf")


def plan_lookup_for(root: Path):
    """premium/free from the bot's quota ledger, read fresh on every call."""
    ledger = None

    def lookup(uid: str) -> str:
        nonlocal ledger
        from message_quota import MessageQuota

        if ledger is None:
            ledger = MessageQuota(root / "message_quota.sqlite3")
        return ledger.plan_for(uid)

    return lookup


def open_user_storage(root: Path):
    from plugins.user_storage.store import UserStorage

    return UserStorage(root / "plugins" / "user_storage", plan_lookup=plan_lookup_for(root))


def inject_shim(body: bytes) -> bytes:
    for pattern in (_HEAD_RE, _HTML_RE, _DOCTYPE_RE):
        found = pattern.search(body, 0, 4096)
        if found:
            return body[: found.end()] + _SHIM + body[found.end():]
    return _SHIM + body


def content_type(path: Path) -> str:
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind.startswith("text/") or kind in _TEXT_TYPES:
        kind += "; charset=utf-8"
    return kind


def _disposition(path: Path, *, inline: bool) -> str:
    name = path.name.replace('"', "")
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_")
    mode = "inline" if inline else "attachment"
    return f"{mode}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


class _GatedFile(web.FileResponse):
    """aiohttp streams the body inside prepare(), so that's where the slot is held."""

    def __init__(self, path, *, gate: asyncio.Semaphore, **kwargs):
        super().__init__(path, **kwargs)
        self._gate = gate

    async def prepare(self, request):
        async with self._gate:
            return await super().prepare(request)


class UserContent:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._storage = None
        self._root: Path | None = None
        self.limit = RateLimiter(rate=20, burst=200)
        try:
            streams = int(os.getenv("MAXWELL_USER_CONTENT_STREAMS", "32"))
        except ValueError:
            streams = 32
        self.streams = asyncio.Semaphore(max(1, min(streams, 512)))

    def storage(self):
        root = Path(self.data_dir())
        if self._storage is None or self._root != root:
            self._storage = open_user_storage(root)
            self._root = root
        return self._storage

    def _admit(self, request) -> None:
        from api.auth import _get_client_ip

        if not self.limit.allow(_get_client_ip(request)):
            raise web.HTTPTooManyRequests(text="slow down", headers={"Retry-After": "5"})

    @staticmethod
    def _headers(extra: dict | None = None) -> dict:
        headers = {
            "Content-Security-Policy": SANDBOX_CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            # Opaque-origin pages fetch their own JSON/modules cross-origin.
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache",
        }
        headers.update(extra or {})
        return headers

    def _resolve_public(self, handle: str, rest: str):
        """(file, redirect) for a public request; both None means 404."""
        from plugins.user_storage.store import PUBLIC_DIR, StorageError, clean_path

        storage = self.storage()
        uid = storage.uid_for_handle(handle)
        # Hosting is a Premium perk. Files survive a lapse; serving pauses.
        if uid is None or storage.quota(uid) <= 0:
            return None, None
        folder = not rest or rest.endswith("/")
        try:
            rel = clean_path(f"{PUBLIC_DIR}/{rest}")
        except StorageError:
            return None, None
        if folder:
            return storage.file_path(uid, rel + "/index.html"), None
        found = storage.file_path(uid, rel)
        if found is not None:
            return found, None
        directory = storage._resolve(storage.user_dir(uid), rel)
        if directory is not None and directory.is_dir():
            return None, quote(rel.rsplit("/", 1)[-1], safe="") + "/"
        return None, None

    async def _send(self, request, path: Path, *, inline: bool, attachment: bool = False):
        kind = content_type(path)
        extra = {"Content-Type": kind}
        if attachment or not inline:
            extra["Content-Disposition"] = _disposition(path, inline=False)
        if kind.startswith("text/html"):
            def load():
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as handle:
                    return handle.read(_SHIM_LIMIT + 1)

            body = await asyncio.to_thread(load)
            if len(body) <= _SHIM_LIMIT:
                return web.Response(body=inject_shim(body), headers=self._headers(extra))
        return _GatedFile(path, gate=self.streams, headers=self._headers(extra))

    async def public(self, request):
        self._admit(request)
        handle = request.match_info["handle"]
        rest = request.match_info.get("path")
        if rest is None:
            raise web.HTTPFound(quote(handle, safe="") + "/")
        found, redirect = await asyncio.to_thread(self._resolve_public, handle, rest)
        if redirect:
            raise web.HTTPFound(redirect)
        if found is None:
            return web.Response(status=404, text="Not found.", headers=self._headers())
        return await self._send(request, found, inline=True)

    async def signed(self, request):
        self._admit(request)
        info = request.match_info

        def resolve():
            storage = self.storage()
            if not storage.verify(info["uid"], info["path"], info["expires"], info["sig"]):
                return None
            return storage.file_path(info["uid"], info["path"])

        found = await asyncio.to_thread(resolve)
        if found is None:
            return web.Response(
                status=404, text="This link expired or the file is gone.", headers=self._headers()
            )
        kind = content_type(found).split(";", 1)[0]
        inline = kind.startswith(_INLINE_PREFIXES) or kind in _INLINE_TYPES
        return await self._send(request, found, inline=inline, attachment=request.query.get("download") == "1")


def register_user_content(app, data_dir) -> UserContent:
    content = UserContent(data_dir)
    for method in ("GET", "HEAD"):
        app.router.add_route(
            method, r"/u/_s/{uid:\d{1,25}}/{expires:\d{1,12}}/{sig:[0-9a-f]{32}}/{path:.+}", content.signed
        )
        app.router.add_route(method, r"/u/{handle:[a-z0-9-]{1,30}}", content.public)
        app.router.add_route(method, r"/u/{handle:[a-z0-9-]{1,30}}/{path:.*}", content.public)
    return content
