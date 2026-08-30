"""Real backends for generated sites: one container per site.

``site_backend.py`` gives a static page a place to keep things. This gives it a
server — the site writes actual Python, it runs as its own process, and it owns
its routes, its database, its secrets, and its outbound calls. Whatever backend
it wants.

The shape:

* Code lives in ``DATA_DIR/site_servers/<slug>/`` — **outside** the web root, so
  the source and any secrets are never served as static files.
* Each site gets a container from the ``maxwell-site-runtime`` image: code
  read-only at ``/app``, a private writable ``/data`` for its database, no
  capabilities, half a core, 256MB, and a port published on 127.0.0.1 only.
* Requests reach it at ``/bot/<slug>/api/...``, which the API server proxies to
  that port (see ``site_proxy`` in api/api_server.py). Nothing else on the box
  can be reached through that path, and the container's port is not exposed
  publicly on its own.
* ``--restart unless-stopped`` means a reboot or a docker restart brings every
  site backend back without the bot having to do anything.

Secrets go in as environment variables and stay in the registry file, which
lives with the rest of the bot's data — never in the site directory, never in
a tool result, never in ``read``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import socket
import uuid
from pathlib import Path
from typing import Any

from utils import FileLock, _atomic_json_write_sync

logger = logging.getLogger(__name__)

IMAGE = "maxwell-site-runtime"
DOCKERFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker", "site-runtime")
CONTAINER_PREFIX = "maxwell-site-"
# Deliberately NOT the same prefix as CONTAINER_PREFIX. When they matched, a
# per-site image and its container shared one identifier, and `docker inspect`
# went on finding the image after the container was removed — so the
# wait-for-removal loop span for its full timeout on every deploy.
IMAGE_PREFIX = "maxwell-siteimg-"
CONTAINER_PORT = 8000

# Ports handed to site backends. Loopback only — the public path is the proxy.
PORT_RANGE = range(8800, 8900)

# Per-container limits. A site backend is a toy web service, not a workload.
MEMORY = "256m"
CPUS = "0.5"
PIDS = "128"

MAX_CODE_BYTES = 400_000
MAX_FILES = 20
MAX_ENV_KEYS = 25
MAX_ENV_VALUE = 4_000
START_TIMEOUT = 25.0
_LIFECYCLE_LOCK = asyncio.Lock()

ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9-]{2,30}$")

# Names a backend must not receive: PATH/HOME shape the runtime, and PORT is
# ours to set.
RESERVED_ENV = {"PATH", "HOME", "PORT", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"}


class SiteServerError(Exception):
    """Something the model can fix by calling again differently."""


def _check_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(str(slug or "")):
        raise SiteServerError("bad site slug")
    return str(slug)


def container_name(slug: str) -> str:
    return CONTAINER_PREFIX + _check_slug(slug)


def code_dir(data_dir, slug: str) -> Path:
    return Path(data_dir) / "site_servers" / _check_slug(slug)


def state_dir(data_dir, slug: str) -> Path:
    """The container's writable /data — its database lives here."""
    return Path(data_dir) / "site_servers" / _check_slug(slug) / "_data"


def registry_path(data_dir) -> Path:
    return Path(data_dir) / "site_servers.json"


# ── registry ──────────────────────────────────────────────────────────────
def _read_registry(data_dir) -> dict[str, dict]:
    try:
        raw = json.loads(registry_path(data_dir).read_text(encoding="utf-8"))
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        UnicodeError,
        ValueError,
    ):
        return {}
    return (
        {
            str(k): v
            for k, v in raw.items()
            if SLUG_RE.fullmatch(str(k)) and isinstance(v, dict)
        }
        if isinstance(raw, dict)
        else {}
    )


def get_entry(data_dir, slug: str) -> dict | None:
    return _read_registry(data_dir).get(str(slug))


def _write_entry(data_dir, slug: str, entry: dict | None) -> None:
    slug = _check_slug(slug)
    path = registry_path(data_dir)
    with FileLock(path, timeout=15.0):
        reg = _read_registry(data_dir)
        if entry is None:
            reg.pop(slug, None)
        else:
            reg[slug] = entry
        _atomic_json_write_sync(path, reg)


def port_for(data_dir, slug: str) -> int | None:
    """The loopback port a slug's backend listens on, for the proxy."""
    entry = get_entry(data_dir, slug)
    if not entry or entry.get("running") is not True:
        return None
    try:
        port = int(entry.get("port") or 0)
    except (TypeError, ValueError):
        return None
    return port if port in PORT_RANGE else None


def _registry_port(value: Any) -> int | None:
    try:
        port = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return port if port in PORT_RANGE else None


def _free_port(data_dir, slug: str) -> int:
    taken = {
        port
        for s, e in _read_registry(data_dir).items()
        if s != slug
        for port in [_registry_port(e.get("port"))]
        if port is not None
    }
    for port in PORT_RANGE:
        if port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise SiteServerError("no free backend ports left — delete an unused site backend")


def _port_is_free(port: int) -> bool:
    if port not in PORT_RANGE:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ── docker ────────────────────────────────────────────────────────────────
async def _docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SiteServerError("docker is not installed or not on PATH") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SiteServerError(f"docker did not respond within {timeout:.0f}s") from None
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def _ensure_image() -> None:
    code, _out, _err = await _docker("image", "inspect", IMAGE, timeout=20)
    if code == 0:
        return
    logger.info("Building %s (first site backend on this host)", IMAGE)
    code, _out, err = await _docker("build", "-t", IMAGE, DOCKERFILE_DIR, timeout=600)
    if code != 0:
        raise SiteServerError(f"could not build the site runtime image: {err.strip()[:300]}")


async def _remove_container(slug: str) -> None:
    """Remove the container and wait until it is really gone.

    ``docker rm -f`` can return while removal is still settling, and a
    container carrying --restart unless-stopped may be mid-restart when we ask.
    Returning early let the next ``docker run`` hit a name conflict, and let
    ``inspect`` read the OLD container's state — which produced confidently
    wrong health diagnoses. So: ask, then confirm it is absent.
    """
    name = container_name(slug)
    _rm_code, _out, rm_err = await _docker("rm", "-f", name, timeout=30)
    for _ in range(100):  # ~20s — docker can be slow while a build is running
        code, _out, _err = await _docker(
            "inspect", "--type", "container", "-f", "{{.Id}}", name, timeout=10
        )
        if code != 0:
            return
        await asyncio.sleep(0.2)
    # Docker can return before a restart/removal has fully settled. Retry
    # regardless of the first exit code; a replacement must not proceed into a
    # name/port collision, and a destroy should make a best effort to kill the
    # service rather than merely deleting its registry row.
    retry_code, _out, retry_err = await _docker("rm", "-f", name, timeout=30)
    if retry_code == 0:
        for _ in range(25):
            code, _out, _err = await _docker(
                "inspect",
                "--type",
                "container",
                "-f",
                "{{.Id}}",
                name,
                timeout=10,
            )
            if code != 0:
                return
            await asyncio.sleep(0.2)
    rm_err = retry_err or rm_err
    raise SiteServerError(
        f"could not remove backend container {name}: "
        f"{(rm_err or 'container is still present').strip()[:300]}"
    )


# ── code ──────────────────────────────────────────────────────────────────
def _safe_rel(raw: Any) -> str | None:
    text = str(raw or "").strip().replace("\\", "/").lstrip("/")
    if not text or len(text) > 120:
        return None
    parts = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if (
            part == ".."
            or part.startswith(".")
            or part in {"_data", "_build"}
        ):
            return None
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", part):
            return None
        parts.append(part)
    if not parts or len(parts) > 3:
        return None
    return "/".join(parts)


def parse_files(files: Any) -> dict[str, str]:
    """Accept {"app.py": "..."} or [{"path":..,"content":..}], as elsewhere."""
    raw = files
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SiteServerError(f"files must be JSON: {e}") from None
    if isinstance(raw, dict) and "path" in raw and "content" in raw:
        raw = [raw]
    if isinstance(raw, list):
        if not all(isinstance(item, dict) for item in raw):
            raise SiteServerError("each server file needs {path, content}")
        pairs = [(i.get("path"), i.get("content")) for i in raw]
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        raise SiteServerError("files must be an object or a list of {path, content}")
    out: dict[str, str] = {}
    for path, content in pairs:
        rel = _safe_rel(path)
        if not rel:
            raise SiteServerError(f"unsafe server file path: {path!r}")
        if not rel.endswith((".py", ".txt", ".json", ".sql", ".html", ".css", ".js")):
            raise SiteServerError(f"{rel}: server files must be .py/.json/.txt/.sql/.html/.css/.js")
        if isinstance(content, str):
            rendered = content
        else:
            try:
                rendered = json.dumps(content, indent=2, allow_nan=False)
            except (TypeError, ValueError):
                raise SiteServerError(f"{rel}: content must be JSON-serializable") from None
        out[rel] = rendered
    if not out:
        raise SiteServerError("no files given")
    if len(out) > MAX_FILES:
        raise SiteServerError(f"too many server files (max {MAX_FILES})")
    total = sum(len(v.encode("utf-8")) for v in out.values())
    if total > MAX_CODE_BYTES:
        raise SiteServerError(f"server code too large ({total} bytes, max {MAX_CODE_BYTES})")
    return out


# A site that needs something outside the baked-in toolkit gets a per-site
# image built FROM the shared one. Pinned or bare names only — no flags, no
# URLs, no git+ssh, nothing that turns a package list into a shell.
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}(\[[A-Za-z0-9,_-]{1,40}\])?(==[A-Za-z0-9._-]{1,20})?$")
MAX_PACKAGES = 15


def parse_packages(packages: Any) -> list[str]:
    if not packages:
        return []
    raw = packages
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [p.strip() for p in raw.replace("\n", ",").split(",")]
    if not isinstance(raw, list):
        raise SiteServerError("packages must be a list of pip names")
    out = []
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if not PACKAGE_RE.fullmatch(name):
            raise SiteServerError(
                f"bad package {name!r} — use a plain pip name, optionally "
                "pinned like 'redis==5.0.1'"
            )
        out.append(name)
    if len(out) > MAX_PACKAGES:
        raise SiteServerError(f"too many packages (max {MAX_PACKAGES})")
    return out


async def build_site_image(data_dir, slug: str, packages: list[str]) -> str:
    """Image for one site: the shared runtime plus its extra packages."""
    slug = _check_slug(slug)
    packages = parse_packages(packages)
    if not packages:
        return IMAGE
    await _ensure_image()
    tag = IMAGE_PREFIX + slug
    build_dir = Path(data_dir) / "site_servers" / slug / "_build"
    build_dir.mkdir(parents=True, exist_ok=True)
    # Package names are validated above, so this cannot inject flags.
    (build_dir / "Dockerfile").write_text(
        f"FROM {IMAGE}\nUSER root\n"
        f"RUN pip install --no-cache-dir {' '.join(packages)}\n"
        "USER site\n",
        encoding="utf-8",
    )
    code, _out, err = await _docker("build", "-t", tag, str(build_dir), timeout=600)
    if code != 0:
        raise SiteServerError(
            "could not install those packages:\n" + (err.strip()[-600:] or "pip failed")
        )
    return tag


async def _remove_site_image(slug: str) -> None:
    await _docker("image", "rm", "-f", IMAGE_PREFIX + _check_slug(slug), timeout=60)


def parse_env(env: Any) -> dict[str, str]:
    if not env:
        return {}
    raw = env
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SiteServerError(f"env must be a JSON object: {e}") from None
    if not isinstance(raw, dict):
        raise SiteServerError("env must be an object of NAME: value")
    out = {}
    for key, value in raw.items():
        key = str(key).strip()
        if not ENV_KEY_RE.fullmatch(key):
            raise SiteServerError(f"bad env name {key!r} — use UPPER_SNAKE_CASE")
        if key in RESERVED_ENV:
            raise SiteServerError(f"{key} is set by the runtime and cannot be overridden")
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, allow_nan=False)
            except (TypeError, ValueError):
                raise SiteServerError(
                    f"{key} must contain a JSON-serializable value"
                ) from None
        if len(text) > MAX_ENV_VALUE:
            raise SiteServerError(f"{key} is too long (max {MAX_ENV_VALUE} chars)")
        out[key] = text
    if len(out) > MAX_ENV_KEYS:
        raise SiteServerError(f"too many env vars (max {MAX_ENV_KEYS})")
    return out


def write_code(data_dir, slug: str, files: dict[str, str]) -> list[str]:
    """Replace the server's source. Returns the paths written."""
    target = code_dir(data_dir, slug)
    target.mkdir(parents=True, exist_ok=True)
    state_dir(data_dir, slug).mkdir(parents=True, exist_ok=True)
    if not isinstance(files, dict) or not files:
        raise SiteServerError("no server files given")
    checked: dict[str, str] = {}
    for rel, content in files.items():
        safe = _safe_rel(rel)
        if not safe:
            raise SiteServerError(f"unsafe server file path: {rel!r}")
        if not safe.endswith(
            (".py", ".txt", ".json", ".sql", ".html", ".css", ".js")
        ):
            raise SiteServerError(
                f"{safe}: server files must be .py/.json/.txt/.sql/.html/.css/.js"
            )
        if not isinstance(content, str):
            raise SiteServerError(f"{safe}: server file content must be a string")
        checked[safe] = content
    if len(checked) > MAX_FILES:
        raise SiteServerError(f"too many server files (max {MAX_FILES})")
    total = sum(len(content.encode("utf-8")) for content in checked.values())
    if total > MAX_CODE_BYTES:
        raise SiteServerError(
            f"server code too large ({total} bytes, max {MAX_CODE_BYTES})"
        )

    # Stage and validate the complete replacement before touching live source.
    # A disk/encoding failure must not leave an otherwise working backend with
    # half of its modules deleted.
    stage = target / f".staging-{uuid.uuid4().hex}"
    backup = target / f".backup-{uuid.uuid4().hex}"
    try:
        stage.mkdir()
        for rel, content in checked.items():
            dest = (stage / rel).resolve()
            if stage.resolve() not in dest.parents:
                raise SiteServerError(f"{rel}: path escapes the server directory")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        backup.mkdir()
        moved_old: list[Path] = []
        moved_new: list[Path] = []
        try:
            # Keep the persistent _data and generated _build directories out of
            # the replacement while moving every other old entry aside.
            for old in target.iterdir():
                if old.name in {"_data", "_build", stage.name, backup.name}:
                    continue
                destination = backup / old.name
                os.replace(old, destination)
                moved_old.append(destination)
            for staged in stage.iterdir():
                destination = target / staged.name
                os.replace(staged, destination)
                moved_new.append(destination)
        except Exception as exc:
            for new in reversed(moved_new):
                with contextlib.suppress(Exception):
                    if new.is_dir() and not new.is_symlink():
                        shutil.rmtree(new)
                    else:
                        new.unlink()
            for old in reversed(moved_old):
                with contextlib.suppress(Exception):
                    os.replace(old, target / old.name)
            raise SiteServerError(f"could not replace server source: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(stage, ignore_errors=True)
        with contextlib.suppress(Exception):
            shutil.rmtree(backup, ignore_errors=True)

    written = sorted(checked)
    # /data is written by uid 10001 inside the container.
    with contextlib.suppress(OSError):
        os.chown(state_dir(data_dir, slug), 10001, 10001)
    return sorted(written)


def read_code(data_dir, slug: str, rel: str) -> str:
    safe = _safe_rel(rel)
    if not safe:
        raise SiteServerError(f"bad path {rel!r}")
    base = code_dir(data_dir, slug).resolve()
    path = (base / safe).resolve()
    if base not in path.parents:
        raise SiteServerError(f"{safe} is not in this site's server")
    if not path.is_file():
        raise SiteServerError(f"{safe} is not in this site's server")
    return path.read_text(encoding="utf-8", errors="replace")


def list_code(data_dir, slug: str) -> list[tuple[str, int]]:
    base = code_dir(data_dir, slug)
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.rglob("*")):
        # _data is the app's database, _build is our generated Dockerfile —
        # neither is source the model wrote, so neither belongs in the listing.
        if (
            path.is_file()
            and not path.is_symlink()
            and not {"_data", "_build"} & set(path.parts)
        ):
            with contextlib.suppress(OSError):
                out.append((str(path.relative_to(base)), path.stat().st_size))
    return out


# ── lifecycle ─────────────────────────────────────────────────────────────
async def _http_ping(port: int) -> str:
    """'ok' when something upstream actually speaks HTTP on this port.

    A plain TCP connect proves nothing: with ``-p`` published, docker-proxy
    accepts the connection whether or not the container process is alive, so
    the old connect-only check called a crash-looping backend healthy. Send a
    real request and require a real status line back.
    """
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=3
        )
        writer.write(b"GET / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await asyncio.wait_for(writer.drain(), timeout=3)
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        # Any status counts — a 404 from the app is still the app answering.
        return "ok" if line.startswith(b"HTTP/") else "no HTTP response"
    except (OSError, asyncio.TimeoutError) as e:
        return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _wait_healthy(port: int, slug: str) -> str:
    """Poll until the backend answers, or say why it never did."""
    deadline = asyncio.get_running_loop().time() + START_TIMEOUT
    last = "timeout"
    while asyncio.get_running_loop().time() < deadline:
        code, out, _err = await _docker(
            "inspect",
            "--type",
            "container",
            "-f",
            "{{.State.Running}} {{.State.Restarting}} {{.RestartCount}} {{.State.ExitCode}}",
            container_name(slug),
            timeout=10,
        )
        if code != 0:
            return "the container disappeared"
        parts = out.split()
        if len(parts) < 2:
            return "could not read container health"
        running, restarting = parts[0] == "true", parts[1] == "true"
        restarts = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        exit_code = parts[3] if len(parts) > 3 else "?"
        # --restart unless-stopped means a broken app flaps instead of staying
        # dead, so a restart count is the signal that it is crash-looping.
        if restarts > 0 or restarting:
            return f"it keeps crashing on startup (exit code {exit_code})"
        if running:
            last = await _http_ping(port)
            if last == "ok":
                return "ok"
        await asyncio.sleep(0.4)
    return last


async def _start_unlocked(
    data_dir,
    slug: str,
    *,
    env: dict[str, str] | None = None,
    packages: list[str] | None = None,
) -> dict:
    """(Re)launch the site's backend container. Returns its registry entry."""
    slug = _check_slug(slug)
    source = code_dir(data_dir, slug)
    if not (source / "app.py").is_file():
        raise SiteServerError(
            "no app.py for this site — write the server first (it must listen on 0.0.0.0:$PORT)"
        )
    await _ensure_image()
    previous = get_entry(data_dir, slug) or {}
    if env is None:
        previous_env = previous.get("env")
        env = (
            {k: str(v) for k, v in previous_env.items()}
            if isinstance(previous_env, dict)
            else {}
        )
    env = parse_env(env)
    if packages is None:
        previous_packages = previous.get("packages")
        packages = list(previous_packages) if isinstance(previous_packages, list) else []
    packages = parse_packages(packages)
    image = await build_site_image(data_dir, slug, packages)

    await _remove_container(slug)
    previous_port = _registry_port(previous.get("port"))
    port = previous_port if previous_port is not None else _free_port(data_dir, slug)
    # The port is reused across restarts, so wait for the old listener to
    # actually stop answering. Without this the first health ping can be
    # served by the container we just removed, and a broken replacement
    # reports itself healthy.
    for _ in range(20):
        if await _http_ping(port) != "ok":
            break
        await asyncio.sleep(0.25)
    if not _port_is_free(port):
        # The old container may have vanished while another local service
        # claimed its port. Never publish a site backend onto that service.
        port = _free_port(data_dir, slug)
    args = [
        "run", "-d",
        "--name", container_name(slug),
        "--label", f"maxwell.site={slug}",
        "--restart", "unless-stopped",
        "--memory", MEMORY,
        "--cpus", CPUS,
        "--pids-limit", PIDS,
        "--security-opt", "no-new-privileges:true",
        "--cap-drop", "ALL",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
        "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}",
        "-v", f"{source.resolve()}:/app:ro",
        "-v", f"{state_dir(data_dir, slug).resolve()}:/data:rw",
        "-e", f"PORT={CONTAINER_PORT}",
        "-e", f"SITE_SLUG={slug}",
        "-e", f"SITE_BASE_PATH=/bot/{slug}/api",
    ]
    for key, value in (env or {}).items():
        args.extend(["-e", f"{key}={value}"])
    args.append(image)

    code, _out, err = await _docker(*args, timeout=60)
    if code != 0:
        if previous:
            failed = dict(previous)
            failed.update(
                {
                    "port": port,
                    "env": env,
                    "packages": packages,
                    "running": False,
                    "health": f"start failed: {err.strip()[:300]}",
                    "container": container_name(slug),
                    "image": image,
                }
            )
            _write_entry(data_dir, slug, failed)
        raise SiteServerError(f"could not start the backend: {err.strip()[:300]}")

    health = await _wait_healthy(port, slug)
    entry = {
        "port": port,
        "env": env or {},
        "packages": packages or [],
        "running": health == "ok",
        "health": health,
        "container": container_name(slug),
        "image": image,
    }
    _write_entry(data_dir, slug, entry)
    if health != "ok":
        # Grab the logs BEFORE tearing it down — they are the only thing that
        # tells the model what to fix. Then remove it, so a broken app is not
        # left crash-looping forever holding a port.
        tail = await logs(data_dir, slug, lines=30)
        with contextlib.suppress(Exception):
            await _remove_container(slug)
        raise SiteServerError(
            f"the backend never came up: {health}.\n"
            "It must listen on 0.0.0.0:$PORT and stay in the foreground.\n"
            f"Its last output:\n{tail}"
        )
    return entry


async def start(
    data_dir,
    slug: str,
    *,
    env: dict[str, str] | None = None,
    packages: list[str] | None = None,
) -> dict:
    # Port selection, container replacement, and registry writes form one
    # lifecycle operation. Serialize them so two overlapping tool calls cannot
    # choose the same port or race through the same container name.
    async with _LIFECYCLE_LOCK:
        return await _start_unlocked(
            data_dir, slug, env=env, packages=packages
        )


async def _stop_unlocked(data_dir, slug: str) -> bool:
    """Stop and remove the container; keep the code, the data, and the env."""
    slug = _check_slug(slug)
    entry = get_entry(data_dir, slug)
    await _remove_container(slug)
    if entry:
        entry = dict(entry)
        entry["running"] = False
        entry["health"] = "stopped"
        _write_entry(data_dir, slug, entry)
    return bool(entry)


async def stop(data_dir, slug: str) -> bool:
    async with _LIFECYCLE_LOCK:
        return await _stop_unlocked(data_dir, slug)


async def _destroy_unlocked(data_dir, slug: str) -> None:
    """Site is gone: container, code, database, secrets, registry row."""
    slug = _check_slug(slug)
    with contextlib.suppress(SiteServerError, Exception):
        await _remove_container(slug)
    with contextlib.suppress(Exception):
        await _remove_site_image(slug)
    with contextlib.suppress(Exception):
        shutil.rmtree(code_dir(data_dir, slug), ignore_errors=True)
    with contextlib.suppress(Exception):
        _write_entry(data_dir, slug, None)


async def destroy(data_dir, slug: str) -> None:
    async with _LIFECYCLE_LOCK:
        await _destroy_unlocked(data_dir, slug)


async def logs(data_dir, slug: str, lines: int = 40) -> str:
    try:
        lines = int(lines or 40)
    except (TypeError, ValueError):
        lines = 40
    lines = max(1, min(lines, 200))
    code, out, err = await _docker(
        "logs", "--tail", str(lines), container_name(slug), timeout=20
    )
    if code != 0:
        return "(no container — the backend is not running)"
    text = (out + err).strip()
    return text[-4000:] if text else "(no output yet)"


async def status(data_dir, slug: str) -> str:
    entry = get_entry(data_dir, slug)
    if not entry:
        return "no backend server for this site"
    code, out, _err = await _docker(
        "inspect", "--type", "container", "-f", "{{.State.Status}} {{.RestartCount}}",
        container_name(slug), timeout=15,
    )
    live = out.strip() if code == 0 else "absent"
    files = ", ".join(f"{n} ({s}B)" for n, s in list_code(data_dir, slug)) or "no files"
    raw_env = entry.get("env")
    secrets = ", ".join(sorted(raw_env)) if isinstance(raw_env, dict) else "none"
    raw_packages = entry.get("packages")
    extra = (
        ", ".join(str(package) for package in raw_packages)
        if isinstance(raw_packages, list)
        else ""
    ) or "none (baked-in toolkit only)"
    return (
        f"container {live} on 127.0.0.1:{entry.get('port')} "
        f"(public path /bot/{slug}/api/...)\nfiles: {files}\nenv: {secrets}\n"
        f"extra packages: {extra}"
    )


async def reconcile(data_dir) -> None:
    """On boot: drop registry rows whose container is gone for good.

    Containers carry --restart unless-stopped, so docker brings them back by
    itself. This only fixes the registry when one was removed out from under
    us (docker prune, manual rm, a site deleted while the bot was down).
    """
    for slug, entry in list(_read_registry(data_dir).items()):
        if entry.get("running") is not True:
            continue
        code, out, _err = await _docker(
            "inspect", "--type", "container", "-f", "{{.State.Running}}",
            container_name(slug), timeout=15,
        )
        alive = code == 0 and out.strip() == "true"
        if alive:
            continue
        if code != 0:
            logger.info("Site backend %s has no container any more; clearing it", slug)
            _write_entry(data_dir, slug, None)
        else:
            try:
                await start(data_dir, slug)
                logger.info("Restarted site backend %s", slug)
            except SiteServerError as e:
                logger.warning("Site backend %s would not restart: %s", slug, e)


# ── what the model is told ────────────────────────────────────────────────
EXAMPLE_APP = '''from flask import Flask, request, jsonify
import sqlite3, os

app = Flask(__name__)
DB = "/data/app.db"          # only /data survives a restart

def db():
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT)")
    return conn

@app.get("/notes")           # the page fetches /bot/<slug>/api/notes
def list_notes():
    rows = db().execute("SELECT id, text FROM notes ORDER BY id DESC").fetchall()
    return jsonify([{"id": r[0], "text": r[1]} for r in rows])

@app.post("/notes")
def add_note():
    conn = db()
    conn.execute("INSERT INTO notes (text) VALUES (?)", (request.json["text"],))
    conn.commit()
    return jsonify(ok=True)

from waitress import serve
serve(app, host="0.0.0.0", port=int(os.environ["PORT"]))
'''


def contract(slug: str) -> str:
    """The rules a site backend has to follow, handed back in tool results."""
    return (
        f"Backend server for {slug}:\n"
        f"  Public path : /bot/{slug}/api/...  ->  your routes, with /bot/{slug}/api stripped.\n"
        f"                A route defined as /notes is reached at /bot/{slug}/api/notes.\n"
        "  Entry       : app.py, listening on 0.0.0.0:$PORT (the runtime sets PORT).\n"
        "  Installed   : python 3.12 + flask, waitress, fastapi, uvicorn, websockets, "
        "sqlalchemy, bcrypt, pyjwt, itsdangerous, requests, httpx, jinja2, pillow, "
        "and the stdlib (sqlite3, json, urllib). Anything else: pass packages=[...].\n"
        "  WebSockets  : supported end to end — use FastAPI + uvicorn (waitress "
        "cannot do sockets). This is how you build multiplayer, live chat, or "
        "anything pushed to clients. SSE and streaming responses work too.\n"
        "  Writable    : /data only, and it persists. /app is read-only. Put the "
        "database at /data/app.db.\n"
        "  Secrets     : pass env={\"API_KEY\": \"...\"} — held outside the site "
        "directory, never served, never echoed back. Read with os.environ.\n"
        "  Outbound    : allowed, so this is where a key-carrying API call belongs.\n"
        "  Limits      : 256MB, half a core, 128 processes, no capabilities, "
        "32MB uploads.\n"
        "  Logs        : site_server(action=logs) — stdout/stderr, your prints included."
    )


EXAMPLE_MULTIPLAYER = '''# Multiplayer / live chat: FastAPI + uvicorn, because waitress cannot do
# WebSockets. The browser connects to  new WebSocket(
#   location.origin.replace("http", "ws") + "/bot/<slug>/api/ws")
import os, json, sqlite3
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
room: list[WebSocket] = []

@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    room.append(sock)
    try:
        while True:
            msg = await sock.receive_text()
            for peer in list(room):        # broadcast to everyone else
                if peer is not sock:
                    try:
                        await peer.send_text(msg)
                    except Exception:
                        room.remove(peer)
    except WebSocketDisconnect:
        pass
    finally:
        if sock in room:
            room.remove(sock)

@app.get("/players")
def players():
    return {"connected": len(room)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
'''
