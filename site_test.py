"""Load a published site the way a browser would, and report what broke.

``fetch_url`` only downloads HTML. JS console errors, failed XHRs, and a
blank white page are invisible that way. This talks to a local Chromium
over CDP when one is installed, and always does a static pass (page status,
linked CSS/JS/images, files missing on disk) so a missing browser still
catches 404s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

logger = logging.getLogger(__name__)

CHROME_CANDIDATES = (
    "chromium-browser",
    "chromium",
    "google-chrome-stable",
    "google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
)

_ASSET_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_SKIP_ASSET_PREFIXES = (
    "data:",
    "javascript:",
    "mailto:",
    "tel:",
    "#",
    "blob:",
)


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        if name.startswith("/"):
            if os.path.isfile(name) and os.access(name, os.X_OK):
                return name
            continue
        path = shutil.which(name)
        if path:
            return path
    return None


def page_url(site_base: str, slug: str, path: str | None = None) -> str:
    """Public URL of one page on an owned site.

    ``site_base`` is ``https://host/bot`` (no trailing slash). ``path`` may be
    a relative file, ``about/``, or the full public URL of this same site.
    Raises ValueError if the path would leave the site.
    """
    slug = str(slug or "").strip().strip("/")
    if not slug:
        raise ValueError("name is required")
    root = f"{str(site_base).rstrip('/')}/{slug}/"
    raw = str(path or "").strip()
    if not raw or raw in {".", "/", "./"}:
        return root
    allowed = urlparse(root)
    prefix = allowed.path if allowed.path.endswith("/") else allowed.path + "/"

    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if parsed.scheme != allowed.scheme or parsed.netloc != allowed.netloc:
            raise ValueError("url is not this site")
        got = parsed.path or "/"
        if got.rstrip("/") != prefix.rstrip("/") and not got.startswith(prefix):
            raise ValueError("url is not this site")
        return f"{parsed.scheme}://{parsed.netloc}{got}"

    if raw.startswith(prefix):
        rel = raw[len(prefix) :]
    elif raw.startswith("/"):
        raise ValueError("path is not this site")
    else:
        rel = raw.lstrip("/")

    slash = rel.replace("\\", "/").endswith("/")
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError("bad path")
    return root + "/".join(parts) + ("/" if slash else "")


def extract_assets(html: str, base_url: str, *, limit: int = 24) -> list[str]:
    """Same-origin (and relative) CSS/JS/image URLs referenced by the page."""
    seen: list[str] = []
    found: set[str] = set()
    base = urlparse(base_url)
    for match in _ASSET_RE.finditer(html or ""):
        raw = (match.group(1) or "").strip()
        if not raw or raw.lower().startswith(_SKIP_ASSET_PREFIXES):
            continue
        absolute = urljoin(base_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc and parsed.netloc != base.netloc:
            continue
        if absolute in found:
            continue
        found.add(absolute)
        seen.append(absolute)
        if len(seen) >= limit:
            break
    return seen


def _asset_relpath(raw: str, slug: str) -> str | None:
    """Map a src/href to a file inside the site directory, or None to skip."""
    text = (raw or "").strip()
    if not text or text.lower().startswith(_SKIP_ASSET_PREFIXES):
        return None
    parsed = urlparse(text)
    path = unquote(parsed.path or "")
    prefix = f"/bot/{slug}/"
    if parsed.scheme in {"http", "https"}:
        if not slug or not path.startswith(prefix):
            return None
        rel = path[len(prefix) :]
    elif path.startswith("/"):
        if path.startswith("/api/"):
            return None
        if slug and path.startswith(prefix):
            rel = path[len(prefix) :]
        else:
            return None
    else:
        rel = path
    rel = rel.strip("/")
    if not rel:
        return None
    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        return None
    return "/".join(parts)


def missing_local_assets(
    html: str, site_dir: str, *, slug: str, limit: int = 24
) -> list[str]:
    """Linked files that are not on disk (relative / same-site only)."""
    missing: list[str] = []
    seen: set[str] = set()
    base = Path(site_dir)
    for match in _ASSET_RE.finditer(html or ""):
        rel = _asset_relpath(match.group(1), slug)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        target = base / rel
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            missing.append(rel)
        if len(missing) >= limit:
            break
    return missing


def html_path_for_url(site_dir: str, slug: str, url: str) -> Path:
    """Which file on disk corresponds to the page we are testing."""
    path = urlparse(url).path or "/"
    prefix = f"/bot/{slug}/"
    if path.startswith(prefix):
        rel = path[len(prefix) :]
    else:
        rel = ""
    if not rel or rel.endswith("/"):
        rel = (rel or "") + "index.html"
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        parts = ["index.html"]
    return Path(site_dir).joinpath(*parts) if parts else Path(site_dir) / "index.html"


def format_report(probe: dict[str, Any]) -> str:
    """Turn a probe dict into the tool result the model reads."""
    lines: list[str] = []
    url = str(probe.get("url") or "")
    title = str(probe.get("title") or "")
    status = probe.get("http_status")
    lines.append(f"SITE TEST {url}".strip())
    if title:
        lines.append(f"Title: {title}")
    if status is not None:
        lines.append(f"HTTP: {status}")
    if probe.get("http_error"):
        lines.append(f"HTTP fetch: {probe['http_error']}")
    if probe.get("browser"):
        lines.append(f"Browser: {probe['browser']}")
    elif probe.get("browser_error"):
        lines.append(f"Browser: unavailable ({probe['browser_error']})")

    errors = list(probe.get("console_errors") or [])
    warnings = list(probe.get("console_warnings") or [])
    page_errors = list(probe.get("page_errors") or [])
    failed = list(probe.get("failed_requests") or [])
    assets = list(probe.get("asset_errors") or [])
    backend = str(probe.get("backend") or "").strip()

    if errors:
        lines.append("Console errors:")
        for item in errors[:20]:
            lines.append(f"  • {item}")
    else:
        lines.append("Console errors: none")
    if page_errors:
        lines.append("Uncaught exceptions:")
        for item in page_errors[:10]:
            lines.append(f"  • {item}")
    if warnings:
        lines.append("Console warnings:")
        for item in warnings[:8]:
            lines.append(f"  • {item}")
    if failed:
        lines.append("Failed requests:")
        for item in failed[:15]:
            lines.append(f"  • {item}")
    if assets:
        lines.append("Broken linked assets:")
        for item in assets[:15]:
            lines.append(f"  • {item}")
    if backend:
        lines.append("Backend:")
        lines.append(backend)

    problems = len(errors) + len(page_errors) + len(failed) + len(assets)
    try:
        if status is not None and int(status) >= 400:
            problems += 1
    except (TypeError, ValueError):
        pass
    if probe.get("http_error"):
        problems += 1
    if problems:
        lines.append(
            f"RESULT: {problems} problem(s). Fix with edit_site (frontend) "
            "or site_server (backend), then site_test again."
        )
    else:
        lines.append(
            "RESULT: page loaded with no console errors or failed requests."
        )
    png = probe.get("screenshot_png")
    if isinstance(png, (bytes, bytearray)) and png:
        import base64

        lines.append(f"Screenshot attached ({len(png)} bytes PNG).")
        lines.append(
            f"__IMAGE_B64__{base64.b64encode(bytes(png)).decode('ascii')}__END_IMAGE_B64__"
        )
    elif probe.get("browser") and not probe.get("browser_error"):
        lines.append("No screenshot (capture failed or disabled).")
    return "\n".join(lines)


def _clip(value: Any, limit: int = 300) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


async def http_get(
    url: str, *, max_bytes: int = 1_000_000
) -> tuple[int | None, bytes, str]:
    """GET ``url`` with a plain session (this is our own published site).

    Returns ``(status, body, error)``. ``error`` is set on network failure.
    """
    timeout = aiohttp.ClientTimeout(total=15, connect=6)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            current = url
            for _ in range(5):
                async with session.get(
                    current,
                    allow_redirects=False,
                    headers={"User-Agent": "MaxwellSiteTest/1"},
                ) as resp:
                    if resp.status in {301, 302, 303, 307, 308}:
                        loc = resp.headers.get("Location")
                        if not loc:
                            return resp.status, b"", ""
                        nxt = urljoin(current, loc)
                        if urlparse(nxt).netloc != urlparse(url).netloc:
                            return resp.status, b"", f"redirect left the site → {nxt}"
                        current = nxt
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            break
                        chunks.append(chunk)
                    return resp.status, b"".join(chunks), ""
            return None, b"", "too many redirects"
    except asyncio.TimeoutError:
        return None, b"", "timed out"
    except Exception as exc:
        return None, b"", f"{type(exc).__name__}: {exc}"


async def check_assets(urls: list[str]) -> list[str]:
    errors: list[str] = []
    for url in urls:
        status, _, err = await http_get(url, max_bytes=64 * 1024)
        if err:
            errors.append(f"{err} {url}")
        elif status is not None and status >= 400:
            errors.append(f"{status} {url}")
    return errors


async def _read_devtools_port(profile: str, proc: asyncio.subprocess.Process) -> int:
    port_path = os.path.join(profile, "DevToolsActivePort")
    for _ in range(50):
        if proc.returncode is not None:
            raise RuntimeError("chromium exited before opening DevTools")
        if os.path.isfile(port_path):
            try:
                first = open(port_path, encoding="utf-8").readline().strip()
                return int(first)
            except (OSError, ValueError):
                pass
        await asyncio.sleep(0.1)
    raise RuntimeError("chromium never wrote DevToolsActivePort")


async def probe_browser(
    url: str,
    *,
    wait: float = 2.0,
    screenshot: bool = True,
    chrome: str | None = None,
) -> dict[str, Any]:
    """Load ``url`` in headless Chromium and collect console/network/screenshot."""
    binary = chrome or find_chrome()
    if not binary:
        return {"browser_error": "no chromium/chrome on PATH"}
    wait = max(0.2, min(float(wait or 2.0), 15.0))
    profile = tempfile.mkdtemp(prefix="maxwell-site-test-")
    proc: asyncio.subprocess.Process | None = None
    result: dict[str, Any] = {"browser": os.path.basename(binary)}
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-background-networking",
            "--hide-scrollbars",
            "--window-size=1280,800",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        port = await _read_devtools_port(profile, proc)
        timeout = aiohttp.ClientTimeout(total=25, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/json/version") as resp:
                meta = await resp.json(content_type=None)
            ws_url = str(meta.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                raise RuntimeError("chromium did not expose a debugger websocket")
            async with session.ws_connect(ws_url, heartbeat=10) as ws:
                collected = await _cdp_session(
                    ws, url, wait=wait, screenshot=screenshot
                )
            result.update(collected)
        return result
    except Exception as exc:
        logger.info("site_test browser probe failed: %s", exc)
        result["browser_error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if proc is not None and proc.returncode is None:
            with suppress(Exception):
                os.killpg(proc.pid, signal.SIGKILL)
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=3)
        shutil.rmtree(profile, ignore_errors=True)


async def _cdp_session(
    ws, url: str, *, wait: float, screenshot: bool
) -> dict[str, Any]:
    msg_id = 0
    pending: dict[int, asyncio.Future] = {}
    console_errors: list[str] = []
    console_warnings: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    request_urls: dict[str, str] = {}
    title = ""
    http_status: int | None = None
    png: bytes | None = None
    loaded = asyncio.Event()

    async def send(method: str, params: dict | None = None) -> Any:
        nonlocal msg_id
        msg_id += 1
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending[msg_id] = fut
        await ws.send_str(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=20)

    def note_failed(item: str) -> None:
        if item.startswith(("data:", "blob:", "chrome:", "chrome-error:", "about:")):
            return
        if item not in failed_requests:
            failed_requests.append(item)

    async def reader():
        nonlocal http_status
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(raw.data)
            except json.JSONDecodeError:
                continue
            if "id" in data and data["id"] in pending:
                fut = pending.pop(data["id"])
                if not fut.done():
                    if "error" in data:
                        fut.set_exception(RuntimeError(str(data["error"])))
                    else:
                        fut.set_result(data.get("result") or {})
                continue
            method = data.get("method")
            params = data.get("params") or {}
            if method == "Runtime.consoleAPICalled":
                level = str(params.get("type") or "log")
                args = params.get("args") or []
                text = " ".join(
                    _clip(
                        a.get("description")
                        or a.get("value")
                        or a.get("unserializableValue")
                    )
                    for a in args
                ) or "(empty)"
                if level in {"error", "assert"}:
                    console_errors.append(text)
                elif level == "warning":
                    console_warnings.append(text)
            elif method == "Runtime.exceptionThrown":
                detail = params.get("exceptionDetails") or {}
                exc = (detail.get("exception") or {}).get("description") or detail.get(
                    "text"
                )
                if exc:
                    page_errors.append(_clip(exc, 400))
            elif method == "Log.entryAdded":
                entry = params.get("entry") or {}
                level = str(entry.get("level") or "")
                text = _clip(entry.get("text") or "")
                if level in {"error", "warning"} and text:
                    if level == "error":
                        console_errors.append(text)
                    else:
                        console_warnings.append(text)
            elif method == "Network.requestWillBeSent":
                req_id = str(params.get("requestId") or "")
                req_url = str((params.get("request") or {}).get("url") or "")
                if req_id and req_url:
                    request_urls[req_id] = req_url
            elif method == "Network.loadingFailed":
                req_id = str(params.get("requestId") or "")
                canceled = bool(params.get("canceled"))
                error_text = str(params.get("errorText") or "failed")
                req_url = request_urls.get(req_id, "")
                if not canceled and req_url:
                    note_failed(f"{error_text} {req_url}")
            elif method == "Network.responseReceived":
                resp = params.get("response") or {}
                status = int(resp.get("status") or 0)
                req_url = str(resp.get("url") or "")
                rtype = str(params.get("type") or "")
                if rtype == "Document" and http_status is None:
                    http_status = status
                if status >= 400 and req_url:
                    note_failed(f"{status} {req_url}")
            elif method == "Page.loadEventFired":
                loaded.set()

    reader_task = asyncio.create_task(reader())
    try:
        await send("Runtime.enable")
        await send("Log.enable")
        await send("Network.enable")
        await send("Page.enable")
        await send("Page.navigate", {"url": url})
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(loaded.wait(), timeout=wait)
        await asyncio.sleep(min(1.5, max(0.3, wait * 0.4)))
        try:
            evaluated = await send(
                "Runtime.evaluate",
                {"expression": "document.title || ''", "returnByValue": True},
            )
            title = str((evaluated.get("result") or {}).get("value") or "")
        except Exception:
            title = ""
        if screenshot:
            try:
                shot = await send(
                    "Page.captureScreenshot",
                    {"format": "png", "fromSurface": True},
                )
                import base64

                raw = str(shot.get("data") or "")
                if raw:
                    png = base64.b64decode(raw)
            except Exception as exc:
                logger.info("site_test screenshot failed: %s", exc)
    finally:
        reader_task.cancel()
        with suppress(Exception):
            await reader_task

    out: dict[str, Any] = {
        "title": title,
        "console_errors": _unique(console_errors),
        "console_warnings": _unique(console_warnings),
        "page_errors": _unique(page_errors),
        "failed_requests": _unique(failed_requests),
    }
    if http_status is not None:
        out["http_status"] = http_status
    if png:
        out["screenshot_png"] = png
    return out
