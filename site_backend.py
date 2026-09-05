"""Server-side storage for generated sites.

A page created with ``create_site`` is a static file, so anything it wants to
remember (a guestbook, a poll, a highscore table, a saved draft) has nowhere
to go. This module is that missing half: a tiny per-site datastore exposed
over the same origin the page is served from, so plain ``fetch()`` in the
page works with no key, no CORS dance, and no external service.

Two shapes, because everything a small site needs is one of them:

* **kv**          — named values. Settings, counters, a whole JSON blob.
* **collections** — append-only lists with ids. Submissions, messages, scores.

Both live in ``DATA_DIR/site_data/<slug>.json`` behind the same FileLock the
rest of the bot uses, so the API process and the bot process can both touch a
store without losing writes.

The endpoints are PUBLIC (a visitor's browser calls them), which is the whole
point and also the whole risk. Everything here is therefore bounded: byte
caps, collection caps, ring-buffered items, and a per-IP token bucket. A site
only gets a store at all when it was created with ``backend=true``.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any

from utils import FileLock, _atomic_json_write_sync

# ── limits ────────────────────────────────────────────────────────────────
# Chosen so a busy toy site is comfortable and a hostile one is boring.
MAX_STORE_BYTES = 10_000_000  # whole store, serialized
MAX_VALUE_BYTES = 1_000_000  # one kv value or one collection item
MAX_KEYS = 200
MAX_COLLECTIONS = 20
MAX_ITEMS_PER_COLLECTION = 1000  # oldest drop out (ring)
MAX_NAME_LEN = 64

SLUG_RE = re.compile(r"^[a-z0-9-]{2,30}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,%d}$" % MAX_NAME_LEN)


class SiteBackendError(Exception):
    """Rejected request. ``status`` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(str(slug or "")))


def _check_name(name: str, what: str) -> str:
    name = str(name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise SiteBackendError(
            f"{what} must be 1-{MAX_NAME_LEN} chars of letters, digits, _ . : -"
        )
    return name


def store_path(data_dir: Path | str, slug: str) -> Path:
    if not valid_slug(slug):
        raise SiteBackendError("bad site slug", 404)
    return Path(data_dir) / "site_data" / f"{slug}.json"


def _blank() -> dict[str, Any]:
    return {"kv": {}, "collections": {}}


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON constant {value!r}")


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        UnicodeError,
        ValueError,
    ):
        return _blank()
    if not isinstance(raw, dict):
        return _blank()
    kv = raw.get("kv")
    cols = raw.get("collections")
    return {
        "kv": kv if isinstance(kv, dict) else {},
        "collections": {
            str(k): [item for item in v if isinstance(item, dict)]
            for k, v in (cols or {}).items()
            if isinstance(v, list)
        }
        if isinstance(cols, dict)
        else {},
    }


def _sized(value: Any) -> int:
    try:
        # The limits are bytes, not Python characters.  Keep this serializer
        # strict and identical to the atomic writer's formatting so values
        # containing emoji cannot bypass a cap and NaN/Infinity never reach
        # the JSON file.
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            ).encode("utf-8")
        )
    except (TypeError, UnicodeError, ValueError):
        raise SiteBackendError("value is not JSON-serializable") from None


def _commit(path: Path, store: dict[str, Any]) -> None:
    if _sized(store) > MAX_STORE_BYTES:
        raise SiteBackendError(
            f"site store full (max {MAX_STORE_BYTES} bytes) — delete some data",
            507,
        )
    _atomic_json_write_sync(path, store)


# ── kv ────────────────────────────────────────────────────────────────────
def kv_get(data_dir, slug: str, key: str | None = None) -> Any:
    store = _read(store_path(data_dir, slug))
    if key is None:
        return store["kv"]
    return store["kv"].get(_check_name(key, "key"))


def kv_set(data_dir, slug: str, key: str, value: Any) -> dict[str, Any]:
    key = _check_name(key, "key")
    if _sized(value) > MAX_VALUE_BYTES:
        raise SiteBackendError(f"value too large (max {MAX_VALUE_BYTES} bytes)")
    path = store_path(data_dir, slug)
    with FileLock(path, timeout=10.0):
        store = _read(path)
        if key not in store["kv"] and len(store["kv"]) >= MAX_KEYS:
            raise SiteBackendError(f"too many keys (max {MAX_KEYS})")
        store["kv"][key] = value
        _commit(path, store)
    return {"key": key, "value": value}


def kv_delete(data_dir, slug: str, key: str) -> bool:
    key = _check_name(key, "key")
    path = store_path(data_dir, slug)
    with FileLock(path, timeout=10.0):
        store = _read(path)
        existed = key in store["kv"]
        store["kv"].pop(key, None)
        _commit(path, store)
    return existed


def kv_bump(data_dir, slug: str, key: str, by: float = 1) -> float:
    """Atomic counter. The reason a visit counter isn't a read-then-write race."""
    key = _check_name(key, "key")
    try:
        by = float(by)
    except (TypeError, ValueError):
        raise SiteBackendError("`by` must be a number") from None
    if not math.isfinite(by):
        raise SiteBackendError("`by` must be a finite number")
    path = store_path(data_dir, slug)
    with FileLock(path, timeout=10.0):
        store = _read(path)
        try:
            current = float(store["kv"].get(key) or 0)
        except (TypeError, ValueError):
            current = 0.0
        if not math.isfinite(current):
            current = 0.0
        if key not in store["kv"] and len(store["kv"]) >= MAX_KEYS:
            raise SiteBackendError(f"too many keys (max {MAX_KEYS})")
        total = current + by
        if not math.isfinite(total):
            raise SiteBackendError("counter result must be a finite number")
        if total.is_integer():
            total = int(total)
        store["kv"][key] = total
        _commit(path, store)
    return total


# ── collections ───────────────────────────────────────────────────────────
def items_list(
    data_dir, slug: str, name: str, limit: int = 100, after: str | None = None
) -> list[dict]:
    name = _check_name(name, "collection")
    items = _read(store_path(data_dir, slug))["collections"].get(name) or []
    if after:
        for idx, item in enumerate(items):
            if str(item.get("id")) == str(after):
                items = items[idx + 1 :]
                break
    try:
        limit = max(1, min(int(limit), MAX_ITEMS_PER_COLLECTION))
    except (TypeError, ValueError):
        limit = 100
    return items[-limit:]


def items_add(data_dir, slug: str, name: str, data: Any) -> dict:
    name = _check_name(name, "collection")
    if _sized(data) > MAX_VALUE_BYTES:
        raise SiteBackendError(f"item too large (max {MAX_VALUE_BYTES} bytes)")
    path = store_path(data_dir, slug)
    with FileLock(path, timeout=10.0):
        store = _read(path)
        cols = store["collections"]
        if name not in cols and len(cols) >= MAX_COLLECTIONS:
            raise SiteBackendError(f"too many collections (max {MAX_COLLECTIONS})")
        bucket = cols.setdefault(name, [])
        item = {
            # The old timestamp-plus-list-length scheme collided whenever a
            # full ring received two writes in the same millisecond (the
            # length stays at the cap after each eviction). IDs are cursors,
            # so collisions can make a client's `after` marker skip data.
            "id": f"{time.time_ns()}-{uuid.uuid4().hex}",
            "at": time.time(),
            "data": data,
        }
        bucket.append(item)
        if len(bucket) > MAX_ITEMS_PER_COLLECTION:
            del bucket[: len(bucket) - MAX_ITEMS_PER_COLLECTION]
        _commit(path, store)
    return item


def items_delete(
    data_dir, slug: str, name: str, item_id: str | None = None, all_items: bool = False
) -> int:
    name = _check_name(name, "collection")
    path = store_path(data_dir, slug)
    with FileLock(path, timeout=10.0):
        store = _read(path)
        bucket = store["collections"].get(name) or []
        before = len(bucket)
        if all_items:
            store["collections"].pop(name, None)
            removed = before
        else:
            if not item_id:
                raise SiteBackendError("pass id= or all=1")
            if name not in store["collections"]:
                return 0
            store["collections"][name] = [
                i for i in bucket if str(i.get("id")) != str(item_id)
            ]
            removed = before - len(store["collections"][name])
        _commit(path, store)
    return removed


# ── whole-store helpers (bot side) ────────────────────────────────────────
def snapshot(data_dir, slug: str) -> dict[str, Any]:
    return _read(store_path(data_dir, slug))


def summarize(data_dir, slug: str) -> str:
    """One human line per store, for tool output."""
    store = _read(store_path(data_dir, slug))
    kv = store["kv"]
    cols = store["collections"]
    if not kv and not cols:
        return "backend store is empty"
    bits = []
    if kv:
        bits.append(f"{len(kv)} key(s): " + ", ".join(sorted(kv)[:12]))
    for cname, items in sorted(cols.items())[:12]:
        bits.append(f"{cname}[{len(items)}]")
    return "; ".join(bits)


def wipe(data_dir, slug: str) -> None:
    path = store_path(data_dir, slug)
    with FileLock(path, timeout=10.0):
        _atomic_json_write_sync(path, _blank())


def destroy(data_dir, slug: str) -> None:
    """Remove the store file entirely (site deleted/expired)."""
    path = store_path(data_dir, slug)
    # Never unlink the sidecar while another process holds it: doing so lets a
    # concurrent writer create a new inode and defeats the lock entirely.
    # Keeping an empty .lock file is harmless and avoids a release/delete race.
    with contextlib.suppress(FileNotFoundError, OSError, TimeoutError):
        with FileLock(path, timeout=10.0):
            path.unlink()


# ── abuse control ─────────────────────────────────────────────────────────
class RateLimiter:
    """Per-key token bucket. Public endpoints, so assume someone will try."""

    def __init__(self, rate: float = 1.0, burst: int = 30, max_keys: int = 4096):
        try:
            parsed_rate = float(rate)
        except (TypeError, ValueError):
            parsed_rate = 0.0
        self.rate = max(0.0, parsed_rate) if math.isfinite(parsed_rate) else 0.0
        try:
            parsed_burst = int(burst)
        except (TypeError, ValueError, OverflowError):
            parsed_burst = 30
        try:
            parsed_max_keys = int(max_keys)
        except (TypeError, ValueError, OverflowError):
            parsed_max_keys = 4096
        self.burst = max(1, parsed_burst)
        self.max_keys = max(1, parsed_max_keys)
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(cost) or cost <= 0:
            return False
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + max(0.0, now - last) * self.rate)
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - cost, now)
        if len(self._buckets) > self.max_keys:
            # Cheap eviction: drop the half that has been idle longest.
            oldest = sorted(self._buckets.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[: len(oldest) // 2]:
                self._buckets.pop(k, None)
        return True


# ── the docs the model hands to itself ────────────────────────────────────
def client_guide(base_path: str) -> str:
    """Endpoint cheat-sheet, embedded in tool output so the page can be written
    against a real contract instead of a guess. ``base_path`` is same-origin."""
    return (
        f"Backend is live at {base_path} (same origin — plain fetch, no key, no CORS):\n"
        f"  GET    {base_path}/kv                    -> {{...all keys}}\n"
        f"  GET    {base_path}/kv?key=NAME           -> {{key,value}}\n"
        f"  PUT    {base_path}/kv                    body {{\"key\":\"NAME\",\"value\":ANY}}\n"
        f"  POST   {base_path}/kv/bump               body {{\"key\":\"NAME\",\"by\":1}} -> {{value}}\n"
        f"  DELETE {base_path}/kv?key=NAME\n"
        f"  GET    {base_path}/items/NAME?limit=100&after=ID -> {{items:[{{id,at,data}}]}}\n"
        f"  POST   {base_path}/items/NAME            body = any JSON -> {{item}}\n"
        f"  DELETE {base_path}/items/NAME?id=ID  (or ?all=1)\n"
        f"Values are JSON, {MAX_VALUE_BYTES // 1000}KB per value, "
        f"{MAX_ITEMS_PER_COLLECTION} items per list (oldest drop off), "
        f"{MAX_STORE_BYTES // 1000}KB per site. Writes are public — anyone with "
        "the URL can post, so don't store secrets and expect junk in open forms."
    )
