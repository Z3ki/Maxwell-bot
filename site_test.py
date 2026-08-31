"""Load a published site the way a browser would, and report what broke.

``fetch_url`` only downloads HTML. JS console errors, failed XHRs, and a
blank white page are invisible that way. This drives a real browser over CDP
and always does a static pass (page status, linked CSS/JS/images, files
missing on disk) so a missing browser still catches 404s.

Two engines, tried in order:

``chromium``  the reference. It reports console errors, uncaught exceptions,
              and failed requests through CDP events, which is what makes a
              broken page diagnosable rather than just "loads".
``obscura``   a 30 MB Rust engine with a real V8. Used when no Chromium is
              installed, or when Chromium fails to start. It does not emit
              ``Runtime.consoleAPICalled`` (verified against 0.2.1), so
              console capture there is done by injecting a recorder before
              navigation and reading it back afterwards.

Browser profiles live under the data dir, not ``/tmp``. Snap Chromium
bind-mounts a private ``/tmp``, so a profile created at ``/tmp/x`` is written
somewhere else entirely and the cleanup deletes an empty directory — which is
how 68 MB of orphaned profiles accumulated before this was fixed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import signal
import socket
import time
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

# Obscura ships a single static binary. An explicit path wins so an operator can
# drop it anywhere; otherwise PATH and the conventional install locations.
OBSCURA_CANDIDATES = (
    "obscura",
    "/usr/local/bin/obscura",
    "/opt/obscura/obscura",
)

# Where browser profiles go. Deliberately NOT /tmp: see the module docstring.
_PROFILE_DIRNAME = "browser-profiles"
# A profile older than this belonged to a probe that died without cleaning up
# (SIGKILL, OOM, container stop). Sweeping them is what keeps the disk flat.
_PROFILE_MAX_AGE_SECONDS = 900.0

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


def _data_root() -> str:
    """Root for browser scratch state, overridable by the same env as the bot."""
    return os.path.abspath(os.environ.get("DATA_DIR", "") or "data")


def profile_root() -> str:
    return os.path.join(_data_root(), _PROFILE_DIRNAME)


def sweep_browser_profiles(*, max_age: float = _PROFILE_MAX_AGE_SECONDS) -> int:
    """Delete stale browser profiles. Returns how many went.

    A probe that is killed rather than returned never reaches its own cleanup,
    so without this the profile directory grows by one per killed probe forever.
    Only directories older than ``max_age`` are touched, so a probe running
    right now is never pulled out from under itself.
    """
    root = profile_root()
    if not os.path.isdir(root):
        return 0
    now = time.time()
    removed = 0
    for name in os.listdir(root):
        if not name.startswith("probe-"):
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < max_age:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("Swept %d stale browser profile(s) from %s", removed, root)
    return removed


def _which_first(candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name.startswith("/"):
            if os.path.isfile(name) and os.access(name, os.X_OK):
                return name
            continue
        path = shutil.which(name)
        if path:
            return path
    return None


def find_chrome() -> str | None:
    return _which_first(CHROME_CANDIDATES)


def find_obscura() -> str | None:
    """The Obscura binary, if one is installed.

    ``MAXWELL_OBSCURA_BIN`` takes priority so a downloaded release can be
    pointed at without putting it on PATH.
    """
    explicit = str(os.environ.get("MAXWELL_OBSCURA_BIN", "") or "").strip()
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        logger.warning("MAXWELL_OBSCURA_BIN=%r is not executable; ignoring", explicit)
    return _which_first(OBSCURA_CANDIDATES)


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
        lines.extend(f"  • {item}" for item in errors[:20])
    else:
        lines.append("Console errors: none")
    if page_errors:
        lines.append("Uncaught exceptions:")
        lines.extend(f"  • {item}" for item in page_errors[:10])
    if warnings:
        lines.append("Console warnings:")
        lines.extend(f"  • {item}" for item in warnings[:8])
    if failed:
        lines.append("Failed requests:")
        lines.extend(f"  • {item}" for item in failed[:15])
    if assets:
        lines.append("Broken linked assets:")
        lines.extend(f"  • {item}" for item in assets[:15])
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

    # A page can return 200 with a clean console and still be nothing but a
    # loading shell — "Initializing…", an empty root div, a canvas that never
    # got its script. That is the shape of every site that was listed as live
    # and did not work, and reporting it as a pass is what let them ship.
    stub = describe_stub(probe)
    if stub:
        lines.append(f"NOT ACTUALLY RENDERED: {stub}")
        problems += 1

    if problems:
        lines.append(
            f"RESULT: {problems} problem(s). Fix with edit_site (frontend) "
            "or site_server (backend), then site_test again. Do not tell the "
            "user it works until this says it loaded clean."
        )
    else:
        lines.append("RESULT: page loaded with no console errors or failed requests.")
    png = probe.get("screenshot_png")
    if isinstance(png, (bytes, bytearray)) and png:
        lines.append(f"Screenshot attached ({len(png)} bytes PNG).")
        lines.append(
            f"__IMAGE_B64__{base64.b64encode(bytes(png)).decode('ascii')}__END_IMAGE_B64__"
        )
    elif probe.get("browser") and not probe.get("browser_error"):
        lines.append("No screenshot (capture failed or disabled).")
    return "\n".join(lines)


# Text that means "the app has not started yet", not "this is the page".
_LOADING_WORDS = (
    "loading",
    "initializing",
    "initialising",
    "please wait",
    "starting",
    "booting",
    "connecting",
    "one moment",
)
# Under this many characters of visible text, with nothing drawn, a page is a
# shell rather than a site. Deliberately low: a legitimately minimal page (a
# single headline, a canvas game with a score readout) must not be flagged.
_MIN_VISIBLE_TEXT = 40


def describe_stub(probe: dict[str, Any]) -> str:
    """Why this page looks like an unfinished shell, or "" if it looks real.

    Only meaningful when the browser actually rendered — with no render there
    is nothing to judge, and guessing from HTML source would flag every
    JS-driven page.
    """
    if not probe.get("browser") or probe.get("browser_error"):
        return ""
    text = probe.get("visible_text")
    if text is None:
        return ""
    text = " ".join(str(text).split())
    drew = bool(probe.get("has_canvas_or_media"))
    lowered = text.lower()

    if not text and not drew:
        return "the page rendered no visible text and drew nothing"
    if len(text) < _MIN_VISIBLE_TEXT and not drew:
        hit = next((w for w in _LOADING_WORDS if w in lowered), "")
        if hit:
            return (
                f"the only thing on the page is a {hit!r} placeholder "
                f"({text!r}) — the real content never rendered"
            )
        return (
            f"the page rendered only {len(text)} characters of text "
            f"({text!r}) and drew nothing"
        )
    if lowered in _LOADING_WORDS or any(
        lowered.rstrip(".… ") == word for word in _LOADING_WORDS
    ):
        return f"the page is still showing {text!r}"
    # Deliberately no "too few elements" rule. Node count adds nothing the
    # text+paint check above does not already catch, and on its own it flags
    # legitimate pages: one <p> holding two paragraphs of prose is a real
    # page. ``rendered_nodes`` is still reported, for diagnosis rather than
    # judgement.
    return ""


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


def _free_loopback_port() -> int:
    """Pick a local TCP port for Chromium's debugger.

    Snap Chromium remaps ``/tmp``, so ``--remote-debugging-port=0`` writes
    ``DevToolsActivePort`` somewhere Python cannot see. Binding the port
    ourselves and polling ``/json/version`` over loopback avoids that.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_devtools(
    port: int, proc: asyncio.subprocess.Process, *, timeout: float = 10.0
) -> str:
    """Return a *page* target websocket URL once Chromium is listening.

    ``/json/version`` exposes the browser-level debugger, which rejects
    ``Runtime.enable``. Page targets from ``/json/list`` (or ``/json/new``)
    are the ones that speak the page CDP methods we need.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last = "timeout"
    http_timeout = aiohttp.ClientTimeout(total=0.8, connect=0.4)
    while asyncio.get_running_loop().time() < deadline:
        if proc.returncode is not None:
            raise RuntimeError("chromium exited before opening DevTools")
        try:
            async with aiohttp.ClientSession(timeout=http_timeout) as session:
                async with session.get(f"http://127.0.0.1:{port}/json/list") as resp:
                    targets = await resp.json(content_type=None)
                if isinstance(targets, list):
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        if target.get("type") not in {"page", "tab"}:
                            continue
                        ws_url = str(target.get("webSocketDebuggerUrl") or "")
                        if ws_url:
                            return ws_url
                async with session.put(
                    f"http://127.0.0.1:{port}/json/new?about:blank"
                ) as resp:
                    created = await resp.json(content_type=None)
                if isinstance(created, dict):
                    ws_url = str(created.get("webSocketDebuggerUrl") or "")
                    if ws_url:
                        return ws_url
            last = "no page target"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0.15)
    raise RuntimeError(f"chromium DevTools did not listen on {port} ({last})")


async def _kill_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Kill a browser and its whole process group, even under cancellation.

    ``suppress(Exception)`` does not catch ``CancelledError``, so the old
    version re-raised out of the ``proc.wait()`` and skipped the caller's
    profile cleanup. Cancellation is a live path here — the same-user interrupt
    cancels an in-flight turn mid-probe — so it is handled explicitly.
    """
    if proc is None or proc.returncode is not None:
        return
    with suppress(Exception):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=3)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        # The SIGKILL above already went to the group; reaping is best-effort.
        pass


def _new_profile_dir() -> str:
    """A fresh browser profile under the data dir.

    Not ``tempfile.mkdtemp()``: the Chromium on this box is the snap wrapper,
    which runs with a private ``/tmp`` bind-mounted elsewhere. A profile at
    ``/tmp/x`` is therefore written to ``/tmp/snap-private-tmp/...`` while
    ``rmtree("/tmp/x")`` deletes an empty directory, and the real profile is
    never removed. A path outside ``/tmp`` is not remapped, so the cleanup in
    ``probe_browser`` actually deletes what the browser wrote.
    """
    root = profile_root()
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"probe-{os.getpid()}-{int(time.time() * 1000)}")
    os.makedirs(path, exist_ok=True)
    return path


# Injected before navigation. Obscura does not emit Runtime.consoleAPICalled
# (checked against 0.2.1), so on that engine this recorder is the only way to
# see a console error. Harmless on Chromium, where CDP reports the same events
# natively and duplicates are collapsed by _unique().
_CONSOLE_SHIM = r"""
(function () {
  if (window.__maxwellProbe) return;
  var cap = { errors: [], warnings: [], exceptions: [], resources: [] };
  window.__maxwellProbe = cap;
  var origError = console.error, origWarn = console.warn;
  function join(args) {
    try { return Array.prototype.slice.call(args).map(String).join(' '); }
    catch (e) { return '(unprintable)'; }
  }
  console.error = function () { cap.errors.push(join(arguments)); return origError.apply(console, arguments); };
  console.warn = function () { cap.warnings.push(join(arguments)); return origWarn.apply(console, arguments); };
  window.addEventListener('error', function (ev) {
    try {
      var t = ev && ev.target;
      if (t && t.tagName && (t.src || t.href)) {
        cap.resources.push(String(t.tagName).toLowerCase() + ' ' + (t.src || t.href));
      } else {
        cap.exceptions.push(String((ev && ev.message) || ev) +
          (ev && ev.filename ? ' @ ' + ev.filename + ':' + ev.lineno : ''));
      }
    } catch (e) {}
  }, true);
  window.addEventListener('unhandledrejection', function (ev) {
    try { cap.exceptions.push('Unhandled promise rejection: ' + String((ev && ev.reason) || ev)); } catch (e) {}
  });
})();
"""

# Read back after the page settles. Answers "is there actually a page here",
# which is what separates a working site from a loading shell.
_RENDER_PROBE = r"""
(function () {
  var cap = window.__maxwellProbe || { errors: [], warnings: [], exceptions: [], resources: [] };
  var text = '';
  try {
    // innerText excludes script/style in Chromium, but Obscura falls back to
    // raw textContent — which would return the page's JavaScript source as
    // "visible text" and make every stub look full of content. Walk the body
    // and skip non-rendered elements explicitly instead of trusting either.
    var skip = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, HEAD: 1, TITLE: 1 };
    var parts = [];
    (function walk(node) {
      if (!node) return;
      if (node.nodeType === 3) {
        if (node.nodeValue) parts.push(node.nodeValue);
        return;
      }
      if (node.nodeType !== 1) return;
      if (skip[node.tagName]) return;
      for (var i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
    })(document.body);
    text = parts.join(' ');
  } catch (e) {}
  var nodes = 0;
  try {
    nodes = document.body
      ? document.body.querySelectorAll('*:not(script):not(style):not(noscript)').length
      : 0;
  } catch (e) {
    try { nodes = document.body ? document.body.querySelectorAll('*').length : 0; } catch (e2) {}
  }
  var drew = false;
  try {
    // Only count things that actually painted. A broken <img> or an
    // empty <canvas> is exactly what a stub page has, so counting the
    // tag alone would call every unfinished page "rendered".
    var imgs = document.querySelectorAll('img');
    for (var k = 0; k < imgs.length && !drew; k++) {
      if (imgs[k].naturalWidth > 1 && imgs[k].naturalHeight > 1) drew = true;
    }
    if (!drew) {
      var cvs = document.querySelectorAll('canvas');
      for (var c = 0; c < cvs.length && !drew; c++) {
        if (cvs[c].width > 1 && cvs[c].height > 1) drew = true;
      }
    }
    if (!drew && document.querySelector('svg, video, iframe')) drew = true;
  } catch (e) {}
  var failed = [];
  try {
    var entries = (performance.getEntriesByType && performance.getEntriesByType('resource')) || [];
    for (var i = 0; i < entries.length && failed.length < 20; i++) {
      var e = entries[i];
      if (e.responseStatus && e.responseStatus >= 400) failed.push(e.responseStatus + ' ' + e.name);
    }
  } catch (e) {}
  return JSON.stringify({
    text: String(text).slice(0, 4000),
    nodes: nodes,
    drew: drew,
    errors: cap.errors.slice(0, 30),
    warnings: cap.warnings.slice(0, 20),
    exceptions: cap.exceptions.slice(0, 20),
    resources: cap.resources.slice(0, 20),
    failed: failed
  });
})()
"""


async def probe_browser(
    url: str,
    *,
    wait: float = 2.0,
    screenshot: bool = True,
    chrome: str | None = None,
) -> dict[str, Any]:
    """Load ``url`` in a real browser and collect console/network/render state.

    Chromium first because its CDP event stream is the richest. Obscura is the
    fallback when Chromium is absent or refuses to start, so a box without a
    300 MB browser still gets JS execution and a screenshot instead of a
    static HTML fetch.
    """
    wait = max(0.2, min(float(wait or 2.0), 15.0))
    errors: list[str] = []

    binary = chrome or find_chrome()
    if binary:
        result = await _probe_with_chromium(
            url, binary=binary, wait=wait, screenshot=screenshot
        )
        if not result.get("browser_error"):
            return result
        errors.append(f"chromium: {result['browser_error']}")
    else:
        errors.append("chromium: not on PATH")

    obscura = find_obscura()
    if obscura:
        result = await _probe_with_obscura(
            url, binary=obscura, wait=wait, screenshot=screenshot
        )
        if not result.get("browser_error"):
            return result
        errors.append(f"obscura: {result['browser_error']}")
    else:
        errors.append("obscura: not installed")

    return {"browser_error": "; ".join(errors)}


async def _probe_with_chromium(
    url: str, *, binary: str, wait: float, screenshot: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"browser": os.path.basename(binary)}
    last_error: Exception | None = None
    common_flags = (
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--hide-scrollbars",
        "--window-size=1280,800",
        "--remote-allow-origins=*",
    )
    # Snap Chromium often dies on --headless=new with a private /tmp; the
    # older --headless flag still speaks CDP. Try new first, then old.
    for headless in ("--headless=new", "--headless"):
        profile = _new_profile_dir()
        proc: asyncio.subprocess.Process | None = None
        try:
            port = _free_loopback_port()
            proc = await asyncio.create_subprocess_exec(
                binary,
                headless,
                *common_flags,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "about:blank",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            ws_url = await _wait_devtools(port, proc)
            timeout = aiohttp.ClientTimeout(total=25, connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(ws_url, heartbeat=10) as ws:
                    collected = await _cdp_session(
                        ws, url, wait=wait, screenshot=screenshot
                    )
                result.update(collected)
            return result
        except Exception as exc:
            last_error = exc
            logger.info("site_test browser probe failed (%s): %s", headless, exc)
        finally:
            await _kill_proc(proc)
            shutil.rmtree(profile, ignore_errors=True)
    result["browser_error"] = (
        f"{type(last_error).__name__}: {last_error}"
        if last_error
        else "chromium probe failed"
    )
    return result


async def _probe_with_obscura(
    url: str, *, binary: str, wait: float, screenshot: bool
) -> dict[str, Any]:
    """Drive Obscura's CDP server.

    Obscura's event surface is narrower than Chromium's: ``Network.*`` and
    ``Page.*`` arrive, but console/exception events do not, and page targets
    must be created through ``Target.createTarget`` + ``attachToTarget`` rather
    than read from ``/json/list`` (the pre-made target there rejects commands
    with "No page"). Hence the injected recorder for console output.
    """
    result: dict[str, Any] = {"browser": "obscura"}
    proc: asyncio.subprocess.Process | None = None
    port = _free_loopback_port()
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "serve",
            "--port",
            str(port),
            # Site tests legitimately target loopback (the local site server),
            # which Obscura blocks by default as SSRF protection.
            "--allow-private-network",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        ws_url = await _wait_obscura(port, proc)
        timeout = aiohttp.ClientTimeout(total=45, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(ws_url, heartbeat=10) as ws:
                collected = await _cdp_session(
                    ws, url, wait=wait, screenshot=screenshot, needs_target=True
                )
            result.update(collected)
        return result
    except Exception as exc:
        logger.info("site_test obscura probe failed: %s", exc)
        result["browser_error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        await _kill_proc(proc)


async def _wait_obscura(
    port: int, proc: asyncio.subprocess.Process, *, timeout: float = 10.0
) -> str:
    """Return Obscura's browser-level websocket once it is listening."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = "timeout"
    http_timeout = aiohttp.ClientTimeout(total=0.8, connect=0.4)
    while asyncio.get_running_loop().time() < deadline:
        if proc.returncode is not None:
            raise RuntimeError("obscura exited before opening its CDP port")
        try:
            async with aiohttp.ClientSession(timeout=http_timeout) as session:
                async with session.get(f"http://127.0.0.1:{port}/json/version") as resp:
                    payload = await resp.json(content_type=None)
            ws_url = str((payload or {}).get("webSocketDebuggerUrl") or "")
            if ws_url:
                return ws_url
            last = "no webSocketDebuggerUrl"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0.15)
    raise RuntimeError(f"obscura did not listen on {port} ({last})")


async def _cdp_session(
    ws, url: str, *, wait: float, screenshot: bool, needs_target: bool = False
) -> dict[str, Any]:
    """Drive one page over CDP and collect everything worth reporting.

    ``needs_target`` is for Obscura, whose browser-level socket requires an
    explicit ``Target.createTarget`` + ``attachToTarget`` and then a
    ``sessionId`` on every command; Chromium's page socket needs neither.
    """
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
    visible_text: str | None = None
    rendered_nodes: int | None = None
    has_canvas_or_media = False
    session_id: str | None = None
    loaded = asyncio.Event()

    async def send(method: str, params: dict | None = None) -> Any:
        nonlocal msg_id
        msg_id += 1
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
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
                text = (
                    " ".join(
                        _clip(
                            a.get("description")
                            or a.get("value")
                            or a.get("unserializableValue")
                        )
                        for a in args
                    )
                    or "(empty)"
                )
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
        if needs_target:
            # Obscura's browser socket: make a page and attach to it. The
            # pre-existing target listed at /json/list rejects page commands
            # with "No page", so it cannot be used.
            created = await send("Target.createTarget", {"url": "about:blank"})
            target_id = str((created or {}).get("targetId") or "")
            if not target_id:
                raise RuntimeError("obscura did not return a targetId")
            attached = await send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
            session_id = str((attached or {}).get("sessionId") or "")
            if not session_id:
                raise RuntimeError("obscura did not return a sessionId")

        await send("Runtime.enable")
        with suppress(Exception):
            # Obscura has no Log domain; its absence must not abort the probe.
            await send("Log.enable")
        await send("Network.enable")
        await send("Page.enable")
        # Install the console recorder BEFORE navigating: an error thrown by an
        # inline script during parse is over before any post-load evaluate.
        with suppress(Exception):
            await send(
                "Page.addScriptToEvaluateOnNewDocument", {"source": _CONSOLE_SHIM}
            )
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

        # Read back what actually rendered, plus whatever the shim caught.
        try:
            probed = await send(
                "Runtime.evaluate",
                {"expression": _RENDER_PROBE, "returnByValue": True},
            )
            payload = (probed.get("result") or {}).get("value")
            snapshot = json.loads(payload) if isinstance(payload, str) else None
            if isinstance(snapshot, dict):
                visible_text = str(snapshot.get("text") or "")
                with suppress(TypeError, ValueError):
                    rendered_nodes = int(str(snapshot.get("nodes") or 0))
                has_canvas_or_media = bool(snapshot.get("drew"))
                console_errors.extend(
                    _clip(item) for item in (snapshot.get("errors") or [])
                )
                console_warnings.extend(
                    _clip(item) for item in (snapshot.get("warnings") or [])
                )
                page_errors.extend(
                    _clip(item, 400) for item in (snapshot.get("exceptions") or [])
                )
                for item in snapshot.get("resources") or []:
                    note_failed(f"failed to load {_clip(item)}")
                for item in snapshot.get("failed") or []:
                    note_failed(_clip(item))
        except Exception as exc:
            logger.debug("site_test render probe failed: %s", exc)

        if screenshot:
            try:
                shot = await send(
                    "Page.captureScreenshot",
                    {"format": "png", "fromSurface": True},
                )
                raw = str(shot.get("data") or "")
                if raw:
                    png = base64.b64decode(raw)
            except Exception as exc:
                logger.info("site_test screenshot failed: %s", exc)
    finally:
        reader_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await reader_task

    out: dict[str, Any] = {
        "title": title,
        "console_errors": _unique(console_errors),
        "console_warnings": _unique(console_warnings),
        "page_errors": _unique(page_errors),
        "failed_requests": _unique(failed_requests),
        "has_canvas_or_media": has_canvas_or_media,
    }
    if visible_text is not None:
        out["visible_text"] = visible_text
    if rendered_nodes is not None:
        out["rendered_nodes"] = rendered_nodes
    if http_status is not None:
        out["http_status"] = http_status
    if png:
        out["screenshot_png"] = png
    return out
