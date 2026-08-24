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


class FakeRequest:
    def __init__(self, body=None, query=None, match=None, method="GET", path="/api/site/x/kv"):
        self._body = body
        self.query = query or {}
        self.match_info = match or {}
        self.headers = {}
        self.remote = "203.0.113.9"
        self.method = method
        self.path = path

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
                "guest": {"user_id": "1", "title": "Guestbook", "backend": True},
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
