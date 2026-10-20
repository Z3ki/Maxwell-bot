"""Where stored files show up on the web. Shared by the bot and the API."""

from __future__ import annotations

import os
from urllib.parse import quote

from .store import PUBLIC_DIR, UserStorage, clean_path


def public_base() -> str:
    return os.getenv("MAXWELL_PUBLIC_BASE_URL", "").strip().rstrip("/")


def _quoted(rel: str) -> str:
    return "/".join(quote(part, safe="") for part in rel.split("/") if part)


def public_root(handle: str | None) -> str:
    base = public_base()
    if not base or not handle:
        return ""
    return f"{base}/u/{handle}/"


def public_url(handle: str | None, path: str) -> str:
    """URL for a file under public/, or '' if it isn't public or no host."""
    rel = clean_path(path)
    if rel != PUBLIC_DIR and not rel.startswith(PUBLIC_DIR + "/"):
        return ""
    root = public_root(handle)
    if not root:
        return ""
    inner = rel[len(PUBLIC_DIR):].lstrip("/")
    if inner.endswith("index.html"):
        inner = inner[: -len("index.html")]
    return root + _quoted(inner) + ("/" if inner.endswith("/") else "")


def signed_url(storage: UserStorage, uid: str, path: str, *, ttl: int) -> tuple[str, int]:
    """Private, expiring download link. Anyone holding it can download."""
    base = public_base()
    if not base:
        return "", 0
    rel = clean_path(path)
    signature, expires = storage.sign(uid, rel, ttl=ttl)
    return f"{base}/u/_s/{uid}/{expires}/{signature}/{_quoted(rel)}", expires
