"""The public per-site backend: /api/site/{slug}/...

These routes are deliberately unauthenticated — a visitor's browser calls them
from the generated page — so the tests care as much about what they REFUSE as
what they store: no store without backend=true, no cross-slug reach, caps
enforced, and the rest of the admin API still locked.
"""

import asyncio
import json

import pytest

import api.api_server as api
import site_backend
from api.auth import _needs_auth


class FakeStreamResponse:
    """Stands in for web.StreamResponse — preparing a real one needs a real
    aiohttp request/transport, and what these tests care about is what the
    proxy writes, in what order."""

    def __init__(self, status=200):
        self.status = status
        self.headers = {}
        self.written = b""
        self._req = None

    async def prepare(self, request):
        self._req = request

    async def write(self, chunk):
        self.written += chunk
        hook = getattr(self._req, "on_write", None)
        if hook:
            hook(chunk)

    async def write_eof(self):
        self.eof = True


class _FakeContent:
    def __init__(self, body):
        self._raw = json.dumps(body).encode() if body is not None else b""

    async def read(self, n=-1):
        return self._raw


class FakeRequest:
    def __init__(self, body=None, query=None, match=None, method="GET", path="/api/site/x/kv"):
        self._body = body
        self.query = query or {}
        self.query_string = "&".join(f"{k}={v}" for k, v in (query or {}).items())
        self.can_read_body = body is not None
        self.content = _FakeContent(body)
        self.match_info = match or {}
        self.headers = {}
        self.remote = "203.0.113.9"
        self.method = method
        self.path = path
        self.on_write = None
        self.content_length = None

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _payload(resp):
    return json.loads(resp.text)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    (tmp_path / "sites.json").write_text(
        json.dumps(
            {
                "guest": {
                    "user_id": "1",
                    "title": "Guestbook",
                    "backend": True,
                    "server": True,
                },
                "plain": {"user_id": "1", "title": "Static"},
            }
        ),
        encoding="utf-8",
    )
    # Fresh buckets so an earlier test's traffic can't rate-limit this one.
    monkeypatch.setattr(api, "_SITE_RATE", site_backend.RateLimiter(rate=100, burst=500))
    monkeypatch.setattr(
        api, "_SITE_READ_RATE", site_backend.RateLimiter(rate=100, burst=500)
    )
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def test_site_routes_are_public_and_the_rest_is_not():
    assert _needs_auth(FakeRequest(path="/api/site/guest/kv")) is False
    assert _needs_auth(FakeRequest(path="/api/site/guest/items/notes")) is False
    assert _needs_auth(FakeRequest(path="/api/control", method="PUT")) is True
    assert _needs_auth(FakeRequest(path="/api/sites", method="DELETE")) is True
    # A slug can't prefix its way out of its own namespace.
    assert _needs_auth(FakeRequest(path="/api/sites?slug=x", method="DELETE")) is True


def test_kv_round_trip(data_dir):
    put = run(
        api.site_kv_put(
            FakeRequest({"key": "theme", "value": {"bg": "#111"}}, match={"slug": "guest"})
        )
    )
    assert put.status == 200
    got = run(api.site_kv_get(FakeRequest(query={"key": "theme"}, match={"slug": "guest"})))
    assert _payload(got)["value"] == {"bg": "#111"}
    everything = run(api.site_kv_get(FakeRequest(match={"slug": "guest"})))
    assert _payload(everything) == {"theme": {"bg": "#111"}}
    gone = run(
        api.site_kv_delete(FakeRequest(query={"key": "theme"}, match={"slug": "guest"}))
    )
    assert _payload(gone)["deleted"] is True


def test_bump_is_an_atomic_counter(data_dir):
    for _ in range(3):
        resp = run(api.site_kv_bump(FakeRequest({"key": "hits"}, match={"slug": "guest"})))
    assert _payload(resp)["value"] == 3
    resp = run(api.site_kv_bump(FakeRequest({"key": "hits", "by": 10}, match={"slug": "guest"})))
    assert _payload(resp)["value"] == 13


def test_collections_append_and_page(data_dir):
    ids = []
    for name in ("ana", "bo", "cy"):
        resp = run(
            api.site_items_post(
                FakeRequest({"name": name}, match={"slug": "guest", "name": "signatures"})
            )
        )
        ids.append(_payload(resp)["item"]["id"])
    listing = run(
        api.site_items_get(FakeRequest(match={"slug": "guest", "name": "signatures"}))
    )
    items = _payload(listing)["items"]
    assert [i["data"]["name"] for i in items] == ["ana", "bo", "cy"]

    after = run(
        api.site_items_get(
            FakeRequest(query={"after": ids[0]}, match={"slug": "guest", "name": "signatures"})
        )
    )
    assert [i["data"]["name"] for i in _payload(after)["items"]] == ["bo", "cy"]

    removed = run(
        api.site_items_delete(
            FakeRequest(query={"id": ids[1]}, match={"slug": "guest", "name": "signatures"})
        )
    )
    assert _payload(removed)["removed"] == 1


def test_items_list_unknown_cursor_is_empty(data_dir):
    for n in range(3):
        site_backend.items_add(data_dir, "guest", "log", n)
    assert site_backend.items_list(data_dir, "guest", "log", after="no-such-id") == []


def test_items_list_after_returns_next_page_not_newest(data_dir):
    ids = []
    for n in range(5):
        ids.append(site_backend.items_add(data_dir, "guest", "paged", n)["id"])
    page = site_backend.items_list(data_dir, "guest", "paged", limit=2, after=ids[0])
    assert [i["data"] for i in page] == [1, 2]
    newest = site_backend.items_list(data_dir, "guest", "paged", limit=2)
    assert [i["data"] for i in newest] == [3, 4]


def test_a_site_without_backend_gets_nothing(data_dir):
    resp = run(api.site_kv_get(FakeRequest(match={"slug": "plain"})))
    assert resp.status == 404
    resp = run(api.site_kv_put(FakeRequest({"key": "a", "value": 1}, match={"slug": "plain"})))
    assert resp.status == 404
    assert not (data_dir / "site_data" / "plain.json").exists()


def test_unknown_and_malformed_slugs_are_refused(data_dir):
    assert run(api.site_kv_get(FakeRequest(match={"slug": "nosuchsite"}))).status == 404
    assert run(api.site_kv_get(FakeRequest(match={"slug": "../../etc"}))).status == 404
    assert run(api.site_kv_get(FakeRequest(match={"slug": ""}))).status == 404


def test_proxy_requires_live_server_metadata(data_dir, monkeypatch):
    monkeypatch.setattr(api.site_server, "port_for", lambda dd, slug: 8801)
    sites_path = data_dir / "sites.json"
    sites = json.loads(sites_path.read_text(encoding="utf-8"))
    sites["guest"]["server"] = False
    sites_path.write_text(json.dumps(sites), encoding="utf-8")
    resp = run(api.site_proxy(FakeRequest(match={"slug": "guest", "path": "notes"})))
    assert resp.status == 404


def test_admin_site_delete_cleans_real_backend(data_dir, tmp_path, monkeypatch):
    site_root = tmp_path / "public"
    (site_root / "guest").mkdir(parents=True)
    (site_root / "guest" / "index.html").write_text("site", encoding="utf-8")
    monkeypatch.setattr(api, "BASE_SITE_DIR", site_root)
    destroyed = []

    async def fake_server_destroy(dd, slug):
        destroyed.append((dd, slug))

    monkeypatch.setattr(api.site_server, "destroy", fake_server_destroy)
    monkeypatch.setattr(api.site_backend, "destroy", lambda dd, slug: None)
    resp = run(api.site_delete(FakeRequest(query={"slug": "guest"})))
    assert resp.status == 200
    assert not (site_root / "guest").exists()
    assert destroyed == [(data_dir, "guest")]


def test_writes_are_rate_limited(data_dir, monkeypatch):
    monkeypatch.setattr(api, "_SITE_RATE", site_backend.RateLimiter(rate=0, burst=2))
    codes = [
        run(
            api.site_kv_bump(FakeRequest({"key": "spam"}, match={"slug": "guest"}))
        ).status
        for _ in range(4)
    ]
    assert codes[:2] == [200, 200]
    assert codes[2:] == [429, 429]


def test_oversized_values_are_rejected(data_dir):
    huge = "x" * (site_backend.MAX_VALUE_BYTES + 10)
    resp = run(api.site_kv_put(FakeRequest({"key": "big", "value": huge}, match={"slug": "guest"})))
    assert resp.status == 400
    assert "too large" in _payload(resp)["error"]


def test_collections_ring_buffer_at_the_cap(data_dir, monkeypatch):
    monkeypatch.setattr(site_backend, "MAX_ITEMS_PER_COLLECTION", 5)
    for n in range(8):
        site_backend.items_add(data_dir, "guest", "log", n)
    kept = [i["data"] for i in site_backend.items_list(data_dir, "guest", "log", limit=99)]
    assert kept == [3, 4, 5, 6, 7]


def test_bad_json_body_is_a_400(data_dir):
    resp = run(api.site_kv_put(FakeRequest(None, match={"slug": "guest"})))
    assert resp.status == 400


def test_counters_reject_non_finite_values(data_dir):
    for by in ("nan", "inf", float("-inf")):
        with pytest.raises(site_backend.SiteBackendError, match="finite"):
            site_backend.kv_bump(data_dir, "guest", "hits", by)


def test_corrupt_collection_entries_are_ignored(data_dir):
    path = site_backend.store_path(data_dir, "guest")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kv": {},
                "collections": {
                    "notes": [
                        {"id": "1", "data": "kept"},
                        "not an item",
                        None,
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    assert site_backend.items_list(data_dir, "guest", "notes") == [
        {"id": "1", "data": "kept"}
    ]


def test_value_caps_count_utf8_bytes(data_dir, monkeypatch):
    monkeypatch.setattr(site_backend, "MAX_VALUE_BYTES", 8)
    with pytest.raises(site_backend.SiteBackendError, match="too large"):
        site_backend.kv_set(data_dir, "guest", "emoji", "😀😀")


# ── the proxy in front of a site's own backend server ─────────────────────
def test_proxy_refuses_when_no_backend_is_running(data_dir, monkeypatch):
    monkeypatch.setattr(api.site_server, "port_for", lambda dd, slug: None)
    resp = run(api.site_proxy(FakeRequest(match={"slug": "guest", "path": "notes"})))
    assert resp.status == 404
    assert "no backend server" in _payload(resp)["error"]


def test_proxy_refuses_a_malformed_slug(data_dir):
    resp = run(api.site_proxy(FakeRequest(match={"slug": "../../etc", "path": ""})))
    assert resp.status == 404


def test_proxy_only_ever_targets_loopback(data_dir, monkeypatch):
    """The registry picks the destination — a slug can't steer it elsewhere."""
    seen = {}
    monkeypatch.setattr(api.site_server, "port_for", lambda dd, slug: 8801)

    class FakeContent:
        async def iter_chunked(self, n):
            yield b'{"ok":'
            yield b"true}"

    class FakeResp:
        status = 200
        headers = {"Content-Type": "application/json", "Transfer-Encoding": "chunked"}
        content = FakeContent()

        def release(self):
            seen["released"] = True

    class FakeSession:
        def __init__(self, **kw):
            pass

        async def request(self, method, url, **kw):
            seen["method"], seen["url"], seen["headers"] = method, url, kw.get("headers")
            return FakeResp()

        async def close(self):
            seen["closed"] = True

    monkeypatch.setattr(api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(api.web, "StreamResponse", FakeStreamResponse)
    resp = run(
        api.site_proxy(
            FakeRequest(
                match={"slug": "guest", "path": "notes/5"},
                query={"q": "x"},
                method="GET",
            )
        )
    )
    assert resp.status == 200
    assert seen["url"] == "http://127.0.0.1:8801/notes/5?q=x"
    # The backend is told where it really lives and who is really calling.
    assert seen["headers"]["X-Site-Slug"] == "guest"
    assert seen["headers"]["X-Forwarded-Prefix"] == "/bot/guest/api"
    # Hop-by-hop headers must not be forwarded either way.
    assert "Host" not in seen["headers"]
    assert "Transfer-Encoding" not in resp.headers
    # The upstream connection is always handed back, streamed or not.
    assert seen["released"] and seen["closed"]
    assert resp.written == b'{"ok":true}'


def test_proxy_streams_instead_of_buffering(data_dir, monkeypatch):
    """SSE and long polling only work if chunks go out as they arrive."""
    monkeypatch.setattr(api.site_server, "port_for", lambda dd, slug: 8801)
    order = []

    class FakeContent:
        async def iter_chunked(self, n):
            for i in range(3):
                order.append(f"upstream{i}")
                yield f"data: {i}\n\n".encode()

    class FakeResp:
        status = 200
        headers = {"Content-Type": "text/event-stream"}
        content = FakeContent()

        def release(self):
            pass

    class FakeSession:
        def __init__(self, **kw):
            pass

        async def request(self, *a, **kw):
            return FakeResp()

        async def close(self):
            pass

    monkeypatch.setattr(api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(api.web, "StreamResponse", FakeStreamResponse)
    req = FakeRequest(match={"slug": "guest", "path": "stream"})
    req.on_write = lambda chunk: order.append("client")
    resp = run(api.site_proxy(req))
    assert resp.headers["Content-Type"] == "text/event-stream"
    # Interleaved, not "read everything then write everything".
    assert order == ["upstream0", "client", "upstream1", "client", "upstream2", "client"]


def test_websocket_upgrade_takes_the_socket_path(data_dir, monkeypatch):
    """Multiplayer depends on this branch being reached, not the HTTP one."""
    monkeypatch.setattr(api.site_server, "port_for", lambda dd, slug: 8801)
    called = {}

    async def fake_ws(request, slug, target):
        called["slug"], called["target"] = slug, target
        return "ws-response"

    monkeypatch.setattr(api, "_proxy_websocket", fake_ws)
    req = FakeRequest(match={"slug": "guest", "path": "ws"}, method="GET")
    req.headers["Upgrade"] = "websocket"
    assert run(api.site_proxy(req)) == "ws-response"
    assert called["slug"] == "guest"
    assert called["target"] == "http://127.0.0.1:8801/ws"


def test_websocket_on_a_site_with_no_backend_is_still_refused(data_dir, monkeypatch):
    monkeypatch.setattr(api.site_server, "port_for", lambda dd, slug: None)
    req = FakeRequest(match={"slug": "guest", "path": "ws"}, method="GET")
    req.headers["Upgrade"] = "websocket"
    resp = run(api.site_proxy(req))
    assert resp.status == 404


def test_oversize_upload_is_refused_before_streaming(data_dir, monkeypatch):
    """Content-Length says no, so 40MB is never pulled through this process."""
    monkeypatch.setattr(api.site_server, "port_for", lambda dd, slug: 8801)
    req = FakeRequest(match={"slug": "guest", "path": "upload"}, method="POST")
    req.content_length = api.SITE_UPLOAD_MAX + 1
    resp = run(api.site_proxy(req))
    assert resp.status == 413
    assert "too large" in _payload(resp)["error"]


def test_site_proxy_paths_are_not_killed_by_api_timeout(monkeypatch):
    """A live /bot/... websocket must outlive the 30s admin-API request cap."""

    async def scenario():
        async def handler(request):
            await asyncio.sleep(0.05)
            return "ok"

        monkeypatch.setattr(api, "_API_REQUEST_TIMEOUT", 0.001)
        req = FakeRequest(path="/bot/demo/api/ws", method="GET")
        return await api._reliability_middleware(req, handler)

    assert run(scenario()) == "ok"
