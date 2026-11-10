"""Guarded automatic takeover/handback for the limited text-only standby.

Backup-side roles (``python -m standby.failover <role>``):

* ``watch``    non-root watchdog plus authenticated HTTPS heartbeat/handback
               listener. It only *requests* actions; it holds no root rights.
* ``actuate``  root-only oneshot started by a path unit. It re-validates every
               request before starting/stopping ``maxwell-standby.service``.
* ``simulate`` run the real probes and decision logic against arbitrary
               targets with short thresholds. Never actuates.
* ``status``   print the watchdog status snapshot.

Main is always the authority. The backup takes over only when main has been
continuously silent (no heartbeat) AND unreachable directly AND independent
external observers also cannot reach it, while the backup's own internet is
healthy. It hands back immediately when main's guard or startup gate claims
ownership, when main reports its bot running, or when main stays reachable.
A health timeout is still not proof that main stopped: see docs/STANDBY.md for
the residual split-brain window this design minimises but cannot eliminate.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hmac
import http.client
import json
import os
import pwd
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .policy import PolicyDenied, parse_json, read_credential

CONFLICT_EXIT = 3
WATCHDOG_USER = "maxwell-failover"
ETC = Path("/etc/maxwell-failover")
ROOT_RUN = Path("/run/maxwell-failover")  # root-written, group readable
WATCH_RUN = Path("/run/maxwell-failover-watchdog")  # watchdog-written
WATCH_STATE = Path("/var/lib/maxwell-failover")
ACTUATOR_STATE = Path("/var/lib/maxwell-failover-actuator")
STANDBY_RUN = Path("/run/maxwell-standby")
STANDBY_ETC = Path("/etc/maxwell-standby")
UNIT = "maxwell-standby.service"
LEASE_ID = re.compile(r"[a-f0-9]{32}")


@dataclass
class Config:
    main_ip: str = "89.117.57.145"
    main_ports: tuple[int, ...] = (443, 22)
    listen_port: int = 8443
    tick_seconds: float = 15.0
    heartbeat_stale_seconds: float = 60.0
    takeover_after_seconds: float = 180.0
    external_lead_seconds: float = 45.0
    external_fallback_after_seconds: float = 900.0
    external_veto_seconds: float = 300.0
    stepdown_after_seconds: float = 60.0
    cooldown_seconds: float = 600.0
    conflict_cooldown_seconds: float = 3600.0
    claim_hold_seconds: float = 600.0
    max_takeovers_per_day: int = 3
    probation_seconds: int = 30
    anchors: tuple[str, ...] = (
        "1.1.1.1:443",
        "8.8.8.8:443",
        "9.9.9.9:443",
        "208.67.222.222:443",
    )
    anchors_required: int = 2
    discord_check_url: str = "https://discord.com/api/v10/gateway"
    external_nodes: tuple[str, ...] = (
        "de1.node.check-host.net",
        "nl1.node.check-host.net",
        "fr2.node.check-host.net",
        "us1.node.check-host.net",
    )
    external_min_answers: int = 2

    @classmethod
    def load(cls, path: Path = ETC / "config.json") -> "Config":
        config = cls()
        try:
            text = path.read_text()
        except FileNotFoundError:
            return config
        data = parse_json(text)
        if not isinstance(data, dict):
            raise PolicyDenied("Failover config must be an object")
        for key, value in data.items():
            if key not in cls.__dataclass_fields__:
                raise PolicyDenied("Unknown failover config key")
            current = getattr(config, key)
            if isinstance(current, tuple):
                if not isinstance(value, list) or not value:
                    raise PolicyDenied("Invalid failover config list")
                value = tuple(value)
            elif isinstance(current, bool) or type(value) is bool:
                raise PolicyDenied("Invalid failover config value")
            elif isinstance(current, (int, float)):
                if not isinstance(value, (int, float)) or value < 0:
                    raise PolicyDenied("Invalid failover config number")
                value = type(current)(value)
            elif not isinstance(value, str):
                raise PolicyDenied("Invalid failover config value")
            setattr(config, key, value)
        if config.heartbeat_stale_seconds < 3 * config.tick_seconds:
            raise PolicyDenied("Heartbeat staleness must cover several ticks")
        return config


def armed(path: Path = ETC / "mode") -> bool:
    """Only the exact word ``auto`` arms automatic takeover. Anything else is manual."""
    try:
        return path.read_text().strip() == "auto"
    except OSError:
        return False


# --------------------------------------------------------------------------
# Pure decision logic (unit tested exhaustively; no I/O).
# --------------------------------------------------------------------------


@dataclass
class Observation:
    now: float
    self_ok: bool
    direct_ok: bool
    heartbeat_age: float | None
    heartbeat_bot_running: bool | None
    standby_active: bool
    lease_mode: str | None  # "automatic" | "observe_only" | None
    exit_status: int | None = None


@dataclass
class State:
    down_since: float | None = None
    up_since: float | None = None
    takeovers: list[float] = field(default_factory=list)
    cooldown_until: float = 0.0
    external_veto_until: float = 0.0
    last_claim_at: float = 0.0
    external_started_at: float = 0.0
    external_result: str | None = None
    external_result_at: float = 0.0
    last_decision: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "State":
        state = cls()
        if isinstance(data, dict):
            for key in cls.__dataclass_fields__:
                if key in data:
                    setattr(state, key, data[key])
        state.takeovers = [float(t) for t in state.takeovers if isinstance(t, (int, float))]
        return state


@dataclass
class Decision:
    action: str  # none | takeover | stepdown | cleanup | external_check
    reason: str


def decide(obs: Observation, st: State, cfg: Config, is_armed: bool) -> Decision:
    """Return the single next action. Mutates ``st`` timers only."""
    now = obs.now
    hb_fresh = (
        obs.heartbeat_age is not None and obs.heartbeat_age <= cfg.heartbeat_stale_seconds
    )
    main_alive = hb_fresh or obs.direct_ok
    st.takeovers = [t for t in st.takeovers if t > now - 86400]

    if obs.lease_mode and not obs.standby_active:
        # Standby exited (conflict detector, crash, observe window ended): clean up.
        if obs.exit_status == CONFLICT_EXIT:
            st.cooldown_until = max(st.cooldown_until, now + cfg.conflict_cooldown_seconds)
            return Decision("cleanup", "standby saw primary's own messages; split brain avoided")
        if obs.lease_mode == "automatic":
            st.cooldown_until = max(st.cooldown_until, now + cfg.cooldown_seconds)
        return Decision("cleanup", "standby process is no longer running")

    if obs.standby_active:
        st.down_since = None
        if obs.lease_mode is None:
            return Decision("none", "standby active under manual fencing; not managed")
        if obs.lease_mode == "observe_only":
            return Decision("none", "observe-only test session (never replies)")
        if now - st.last_claim_at < 5:
            return Decision("stepdown", "primary claimed ownership")
        if hb_fresh and obs.heartbeat_bot_running:
            return Decision("stepdown", "primary bot reported running")
        if obs.direct_ok and not (hb_fresh and obs.heartbeat_bot_running is False):
            st.up_since = st.up_since or now
            if now - st.up_since >= cfg.stepdown_after_seconds:
                return Decision("stepdown", "primary reachable again")
            return Decision("none", "primary reachable; confirming before handback")
        st.up_since = None
        if hb_fresh:
            return Decision("none", "primary host back but bot not running; holding until its gate claims")
        return Decision("none", "serving as standby; primary still down")

    st.up_since = None
    if not obs.self_ok:
        st.down_since = None
        return Decision("none", "backup's own internet unhealthy; observations untrusted")
    if main_alive:
        st.down_since = None
        st.external_result = None
        return Decision("none", "primary healthy")
    if st.down_since is None:
        st.down_since = now
        st.external_result = None
    elapsed = now - st.down_since
    external_fresh = (
        st.external_result is not None
        and st.external_result_at >= st.down_since
        and now - st.external_result_at <= 120
    )
    if external_fresh and st.external_result == "up":
        st.external_veto_until = now + cfg.external_veto_seconds
        st.down_since = None
        st.external_result = None
        return Decision("none", "external observers reach primary: partition, not outage")
    want_external = (
        not external_fresh
        and elapsed >= cfg.takeover_after_seconds - cfg.external_lead_seconds
        and now - st.external_started_at >= 60
    )
    if elapsed < cfg.takeover_after_seconds:
        if want_external:
            return Decision("external_check", "pre-confirming outage with external observers")
        return Decision("none", f"primary down {int(elapsed)}s; waiting")
    if not is_armed:
        return Decision("none", "primary appears down, but automatic mode is OFF (manual)")
    if now < st.cooldown_until:
        return Decision("none", "takeover cooldown active")
    if now < st.external_veto_until:
        return Decision("none", "recent external evidence that primary is up")
    if now - st.last_claim_at < cfg.claim_hold_seconds:
        return Decision("none", "primary recently claimed ownership")
    if len(st.takeovers) >= cfg.max_takeovers_per_day:
        return Decision("none", "daily automatic takeover limit reached; manual only")
    if external_fresh and st.external_result == "down":
        return Decision("takeover", "primary silent, unreachable directly and externally")
    if elapsed >= cfg.external_fallback_after_seconds:
        return Decision("takeover", "extended outage; external observers unavailable")
    if want_external:
        return Decision("external_check", "confirming outage with external observers")
    return Decision("none", "waiting for external confirmation")


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def tcp_ok(target: str, timeout: float = 4.0) -> bool:
    host, _, port = target.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def https_status(url: str, timeout: float = 6.0) -> int | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    connection = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443, timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("GET", parsed.path or "/", headers={"User-Agent": "maxwell-failover/1"})
        response = connection.getresponse()
        response.read(65536)
        return response.status
    except (OSError, http.client.HTTPException, ssl.SSLError):
        return None
    finally:
        connection.close()


class Prober:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

    def self_ok(self) -> bool:
        discord = self.pool.submit(https_status, self.cfg.discord_check_url)
        anchors = [self.pool.submit(tcp_ok, a) for a in self.cfg.anchors]
        reached = sum(1 for f in anchors if f.result())
        return discord.result() == 200 and reached >= self.cfg.anchors_required

    def direct_ok(self) -> bool:
        futures = [
            self.pool.submit(tcp_ok, f"{self.cfg.main_ip}:{port}") for port in self.cfg.main_ports
        ]
        return any(f.result() for f in futures)


def classify_external(results: list[Any], min_answers: int = 2) -> str:
    """check-host.net TCP result maps -> ``up`` | ``down`` | ``unknown``.

    ``up`` if any observer reached any main port. ``down`` only when at least
    ``min_answers`` distinct observers answered and every answer was a failure.
    Pending (null) or malformed rows never count as evidence.
    """
    answered: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        for node, rows in result.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if "error" not in row and ("time" in row or "address" in row):
                    return "up"
                if "error" in row:
                    answered.add(str(node))
    return "down" if len(answered) >= max(min_answers, 1) else "unknown"


def external_check(cfg: Config, *, fetch: Callable[[str], Any] | None = None,
                   wait: float = 16.0, poll: float = 4.0) -> str:
    """Ask independent public observers (check-host.net) to TCP-probe main.

    Any failure of the observer service itself yields ``unknown``, which never
    counts as outage evidence.
    """

    def default_fetch(url: str) -> Any:
        parsed = urllib.parse.urlsplit(url)
        connection = http.client.HTTPSConnection(
            "check-host.net", 443, timeout=12, context=ssl.create_default_context()
        )
        try:
            connection.request(
                "GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                headers={"Accept": "application/json", "User-Agent": "maxwell-failover/1"},
            )
            response = connection.getresponse()
            body = response.read(65536)
            if response.status != 200:
                raise OSError("observer unavailable")
            return json.loads(body)
        finally:
            connection.close()

    fetch = fetch or default_fetch
    try:
        ids = []
        for port in cfg.main_ports:
            query = urllib.parse.urlencode(
                [("host", f"{cfg.main_ip}:{port}")] + [("node", n) for n in cfg.external_nodes]
            )
            created = fetch("https://check-host.net/check-tcp?" + query)
            if not isinstance(created, dict) or not isinstance(created.get("request_id"), str):
                return "unknown"
            ids.append(created["request_id"])
        deadline = time.monotonic() + wait
        verdict = "unknown"
        while True:
            if poll:
                time.sleep(poll)
            results = [
                fetch("https://check-host.net/check-result/" + urllib.parse.quote(i, safe=""))
                for i in ids
            ]
            verdict = classify_external(results, cfg.external_min_answers)
            complete = all(
                isinstance(r, dict) and r and all(v is not None for v in r.values()) for r in results
            )
            if verdict == "up" or complete or time.monotonic() >= deadline:
                return verdict
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------
# Small file helpers
# --------------------------------------------------------------------------


def atomic_write(path: Path, data: dict[str, Any], mode: int = 0o640) -> None:
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def read_small_json(path: Path, *, owner: int | None = None, limit: int = 65536) -> Any:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise PolicyDenied("Unexpected failover state file")
    if owner is not None and info.st_uid != owner:
        raise PolicyDenied("Unexpected failover state owner")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return parse_json(stream.read(limit + 1))


def systemctl_show(unit: str, runner=subprocess.run) -> dict[str, str]:
    out = runner(
        ["systemctl", "show", unit, "-p", "ActiveState", "-p", "MainPID", "-p", "ExecMainStatus"],
        capture_output=True, text=True, timeout=20,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


# --------------------------------------------------------------------------
# Root actuator
# --------------------------------------------------------------------------


@dataclass
class ActuatorPaths:
    root_run: Path = ROOT_RUN
    watch_run: Path = WATCH_RUN
    state: Path = ACTUATOR_STATE
    standby_run: Path = STANDBY_RUN
    standby_etc: Path = STANDBY_ETC
    mode: Path = ETC / "mode"
    config: Path = ETC / "config.json"


def _standby_inactive(runner) -> bool:
    shown = systemctl_show(UNIT, runner)
    return shown.get("ActiveState") in {"inactive", "failed"} and shown.get("MainPID") == "0"


def _owner_state(paths: ActuatorPaths) -> str | None:
    for name in ("owner.json", "activation.json"):
        candidate = paths.standby_run / name
        if candidate.exists() or candidate.is_symlink():
            try:
                record = parse_json(read_credential(candidate))
                return str(record.get("state")) if isinstance(record, dict) else "unknown"
            except Exception:
                return "unknown"
    return None


def _clear_auto(paths: ActuatorPaths) -> None:
    for name in ("activation.json", "owner.json"):
        candidate = paths.standby_run / name
        with contextlib.suppress(FileNotFoundError):
            os.unlink(candidate)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(paths.root_run / "lease.json")
    with contextlib.suppress(FileNotFoundError):
        fd = os.open(paths.standby_run, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def stepdown(paths: ActuatorPaths, runner=subprocess.run) -> str:
    owner = _owner_state(paths)
    if owner not in {None, "auto_lease"}:
        raise PolicyDenied("Standby is under manual fencing; use the manual failback")
    runner(["systemctl", "stop", UNIT], check=False, timeout=60, capture_output=True)
    if not _standby_inactive(runner):
        raise PolicyDenied("Standby did not stop; lease retained")
    _clear_auto(paths)
    return "standby stopped and automatic lease cleared"


def takeover(
    paths: ActuatorPaths, request: dict[str, Any], runner=subprocess.run,
    validate: Callable[[Path], None] | None = None, now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    observe = request.get("observe_only") is True
    cfg = Config.load(paths.config)
    if not observe and not armed(paths.mode):
        raise PolicyDenied("Automatic mode is off")
    if (paths.root_run / "lease.json").exists() or _owner_state(paths) is not None:
        raise PolicyDenied("A standby lease or manual fence already exists")
    if not _standby_inactive(runner):
        raise PolicyDenied("Standby already running")
    history_path = paths.state / "history.json"
    try:
        history = [t for t in read_small_json(history_path) if isinstance(t, (int, float)) and t > now - 86400]
    except FileNotFoundError:
        history = []
    if not observe and len(history) >= cfg.max_takeovers_per_day:
        raise PolicyDenied("Daily automatic takeover limit reached")
    if validate is None:
        from .preflight import validate as offline_validate

        def validate(root: Path) -> None:
            # Long outages keep the last-good snapshot: no freshness limit here.
            offline_validate(root, max_age=None)
    validate(paths.standby_etc)
    lease_id = uuid.uuid4().hex
    record = {
        "state": "auto_lease",
        "lease_id": lease_id,
        "mode": "observe_only" if observe else "automatic",
        "takeover_at": now,
        "probation_seconds": int(min(max(cfg.probation_seconds, 0), 120)),
        "observe_seconds": int(min(max(int(request.get("observe_seconds", 90)), 10), 300)),
        "reason": str(request.get("reason", ""))[:200],
    }
    paths.standby_run.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(paths.standby_run / "activation.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    atomic_write(paths.root_run / "lease.json", {k: record[k] for k in ("lease_id", "mode", "takeover_at", "reason")})
    if not observe:
        history.append(now)
        paths.state.mkdir(mode=0o700, exist_ok=True)
        atomic_write(history_path, history, 0o600)  # type: ignore[arg-type]
    started = runner(["systemctl", "start", UNIT], check=False, timeout=90, capture_output=True)
    shown = systemctl_show(UNIT, runner)
    if started.returncode != 0 or shown.get("ActiveState") not in {"active", "activating"}:
        runner(["systemctl", "stop", UNIT], check=False, timeout=60, capture_output=True)
        _clear_auto(paths)
        raise PolicyDenied("Standby failed to start; lease cleared")
    return f"standby started ({record['mode']})"


def actuate(
    paths: ActuatorPaths | None = None, runner=subprocess.run, now: Callable[[], float] = time.time
) -> list[dict[str, Any]]:
    paths = paths or ActuatorPaths()
    if os.geteuid() != 0:
        raise PolicyDenied("Actuator must run as root")
    watchdog_uid = pwd.getpwnam(WATCHDOG_USER).pw_uid
    paths.root_run.mkdir(mode=0o750, exist_ok=True)
    os.chown(paths.root_run, 0, pwd.getpwnam(WATCHDOG_USER).pw_gid)
    os.chmod(paths.root_run, 0o2750)  # setgid: state files inherit the watchdog group
    results = []
    with open(paths.root_run / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for _ in range(5):
            try:
                request = read_small_json(paths.watch_run / "request.json", owner=watchdog_uid, limit=4096)
            except FileNotFoundError:
                break
            try:
                last = read_small_json(paths.root_run / "result.json").get("id")
            except Exception:
                last = None
            if not isinstance(request, dict) or request.get("id") == last:
                break
            result = handle_request(paths, request, runner, now())
            atomic_write(paths.root_run / "result.json", result)
            results.append(result)
    return results


def handle_request(paths: ActuatorPaths, request: dict[str, Any], runner, now: float) -> dict[str, Any]:
    rid = request.get("id")
    result = {"id": rid, "ok": False, "action": request.get("action"), "at": now, "detail": ""}
    try:
        if not isinstance(rid, str) or not LEASE_ID.fullmatch(rid):
            raise PolicyDenied("Invalid request id")
        created = request.get("created_at")
        if not isinstance(created, (int, float)) or not -5 <= now - created <= 60:
            raise PolicyDenied("Stale request")
        action = request.get("action")
        if action == "takeover":
            result["detail"] = takeover(paths, request, runner, now=now)
        elif action in {"stepdown", "cleanup"}:
            result["detail"] = stepdown(paths, runner)
        else:
            raise PolicyDenied("Unknown action")
        result["ok"] = True
    except PolicyDenied as error:
        result["detail"] = str(error)
    except Exception as error:  # never leak details beyond the type
        result["detail"] = "actuator error: " + type(error).__name__
    return result


# --------------------------------------------------------------------------
# Watchdog (non-root) and authenticated listener
# --------------------------------------------------------------------------


class Watchdog:
    def __init__(self, cfg: Config, *, key: str = "", prober: Prober | None = None,
                 watch_run: Path = WATCH_RUN, root_run: Path = ROOT_RUN,
                 state_path: Path = WATCH_STATE / "state.json", mode_path: Path = ETC / "mode",
                 runner=subprocess.run, actuate_requests: bool = True):
        self.cfg, self.key = cfg, key
        self.prober = prober or Prober(cfg)
        self.watch_run, self.root_run = watch_run, root_run
        self.state_path, self.mode_path = state_path, mode_path
        self.runner = runner
        self.actuate_requests = actuate_requests
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.heartbeat_at: float | None = None
        self.heartbeat_bot_running: bool | None = None
        try:
            self.state = State.from_dict(read_small_json(state_path))
        except Exception:
            self.state = State()
        self.status: dict[str, Any] = {}

    # -- inputs from main --
    def record_heartbeat(self, bot_running: bool, claim: bool = False) -> None:
        with self.lock:
            self.heartbeat_at = time.monotonic()
            self.heartbeat_bot_running = bot_running
            if claim:
                self.state.last_claim_at = time.time()

    def lease(self) -> dict[str, Any] | None:
        try:
            data = read_small_json(self.root_run / "lease.json", owner=0)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def request(self, action: str, reason: str, *, wait: float = 45.0, **extra: Any) -> dict[str, Any]:
        with self.request_lock:
            rid = uuid.uuid4().hex
            atomic_write(self.watch_run / "request.json",
                         {"id": rid, "action": action, "reason": reason, "created_at": time.time(), **extra})
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                try:
                    result = read_small_json(self.root_run / "result.json", owner=0)
                    if isinstance(result, dict) and result.get("id") == rid:
                        return result
                except Exception:
                    pass
                time.sleep(0.5)
            return {"id": rid, "ok": False, "detail": "actuator did not answer"}

    def handback(self) -> dict[str, Any]:
        self.record_heartbeat(True, claim=True)
        shown = systemctl_show(UNIT, self.runner)
        active = shown.get("ActiveState") in {"active", "activating", "deactivating"}
        lease = self.lease()
        if not active and lease is None:
            return {"stepped_down": True, "standby_active": False}
        if lease is None:
            return {"stepped_down": False, "standby_active": True, "manual": True}
        result = self.request("stepdown", "primary claimed ownership")
        return {"stepped_down": bool(result.get("ok")), "standby_active": not result.get("ok"),
                "detail": result.get("detail", "")}

    # -- loop --
    def observe(self) -> Observation:
        with self.lock:
            hb_age = None if self.heartbeat_at is None else time.monotonic() - self.heartbeat_at
            hb_bot = self.heartbeat_bot_running
        shown = systemctl_show(UNIT, self.runner)
        lease = self.lease()
        try:
            exit_status = int(shown.get("ExecMainStatus", ""))
        except ValueError:
            exit_status = None
        direct = self.prober.direct_ok()
        return Observation(
            now=time.time(),
            self_ok=self.prober.self_ok(),
            direct_ok=direct,
            heartbeat_age=hb_age,
            heartbeat_bot_running=hb_bot,
            standby_active=shown.get("ActiveState") in {"active", "activating", "deactivating"},
            lease_mode=(lease or {}).get("mode"),
            exit_status=exit_status,
        )

    def tick(self) -> Decision:
        obs = self.observe()
        with self.lock:
            decision = decide(obs, self.state, self.cfg, armed(self.mode_path))
        if decision.action == "external_check":
            self.state.external_started_at = time.time()
            self.state.external_result = external_check(self.cfg)
            self.state.external_result_at = time.time()
            with self.lock:
                decision = decide(self.observe(), self.state, self.cfg, armed(self.mode_path))
            if decision.action == "external_check":
                decision = Decision("none", "external check inconclusive; waiting")
        result = None
        if decision.action in {"takeover", "stepdown", "cleanup"} and self.actuate_requests:
            result = self.request(decision.action, decision.reason)
            if decision.action == "takeover" and result.get("ok"):
                self.state.takeovers.append(time.time())
                self.state.down_since = None
            if decision.action in {"stepdown", "cleanup"} and result.get("ok"):
                self.state.up_since = None
        self.state.last_decision = decision.action + ": " + decision.reason
        self.status = {
            "at": obs.now,
            "armed": armed(self.mode_path),
            "observation": asdict(obs),
            "decision": asdict(decision),
            "result": result,
            "state": asdict(self.state),
        }
        with contextlib.suppress(OSError):
            atomic_write(self.watch_run / "status.json", self.status, 0o644)
        with contextlib.suppress(OSError):
            atomic_write(self.state_path, asdict(self.state), 0o600)
        return decision

    def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                decision = self.tick()
                print(json.dumps({"decision": decision.action, "reason": decision.reason}), flush=True)
            except Exception as error:
                print(json.dumps({"error": type(error).__name__}), flush=True)
            time.sleep(max(1.0, self.cfg.tick_seconds - (time.monotonic() - started)))


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler, context: ssl.SSLContext, slots: int = 8):
        super().__init__(address, handler)
        self.context = context
        self.slots = threading.BoundedSemaphore(slots)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(5)
            tls = self.context.wrap_socket(request, server_side=True)
            tls.settimeout(5)
            self.finish_request(tls, client_address)
            self.shutdown_request(tls)
        except Exception:
            self.shutdown_request(request)
        finally:
            self.slots.release()


def make_handler(watchdog: Watchdog):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MaxwellFailover/1"
        sys_version = ""

        def log_message(self, *args):
            pass

        def reply(self, status: int, obj: dict[str, Any]) -> None:
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            self.close_connection = True
            supplied = self.headers.get("X-Failover-Key", "")
            if self.path != "/failover" or not watchdog.key or not hmac.compare_digest(
                supplied.encode(), watchdog.key.encode()
            ):
                return self.reply(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    return self.reply(413, {"error": "invalid body size"})
                body = parse_json(self.rfile.read(length).decode())
                if not isinstance(body, dict):
                    raise PolicyDenied("object required")
                op = body.get("op")
                bot_running = body.get("bot_running")
                if not isinstance(bot_running, bool):
                    raise PolicyDenied("bot_running must be boolean")
                answer: dict[str, Any] = {"ok": True}
                if op == "heartbeat":
                    watchdog.record_heartbeat(bot_running)
                elif op == "handback":
                    answer.update(watchdog.handback())
                elif op != "status":
                    raise PolicyDenied("unknown op")
                lease = watchdog.lease()
                shown = systemctl_show(UNIT, watchdog.runner)
                answer.setdefault(
                    "standby_active", shown.get("ActiveState") in {"active", "activating", "deactivating"}
                )
                answer["lease_mode"] = (lease or {}).get("mode")
                answer["armed"] = armed(watchdog.mode_path)
                return self.reply(200, answer)
            except Exception:
                return self.reply(400, {"error": "request failed"})

    return Handler


def serve(watchdog: Watchdog, cert: Path, key: Path, port: int) -> BoundedServer:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    server = BoundedServer(("0.0.0.0", port), make_handler(watchdog), context)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="role", required=True)
    sub.add_parser("watch")
    sub.add_parser("actuate")
    sub.add_parser("status")
    sim = sub.add_parser("simulate", help="real probes, short thresholds, never actuates")
    sim.add_argument("--main-ip", required=True)
    sim.add_argument("--anchor", action="append")
    sim.add_argument("--ticks", type=int, default=8)
    sim.add_argument("--tick-seconds", type=float, default=3)
    sim.add_argument("--heartbeat", choices=["none", "running", "stopped"], default="none")
    sim.add_argument("--armed", action="store_true")
    sim.add_argument("--external", choices=["live", "down", "up", "unknown"], default="live")
    obs = sub.add_parser("observe-test", help="root: request a bounded observe-only gateway session")
    obs.add_argument("--seconds", type=int, default=90)
    args = parser.parse_args()

    if args.role == "watch":
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        cfg = Config.load()
        watchdog = Watchdog(cfg, key=read_credential(credentials / "failover-key").strip())
        serve(watchdog, credentials / "tls-cert", credentials / "tls-key", cfg.listen_port)
        watchdog.run_forever()
    elif args.role == "actuate":
        for result in actuate():
            print(json.dumps(result), flush=True)
    elif args.role == "status":
        print((WATCH_RUN / "status.json").read_text())
    elif args.role == "observe-test":
        if os.geteuid() != 0:
            raise SystemExit("root required")
        request = {"id": uuid.uuid4().hex, "action": "takeover", "observe_only": True,
                   "observe_seconds": args.seconds, "reason": "operator observe-only test",
                   "created_at": time.time()}
        print(json.dumps(handle_request(ActuatorPaths(), request, subprocess.run, time.time())))
    else:
        cfg = Config(main_ip=args.main_ip, tick_seconds=args.tick_seconds,
                     heartbeat_stale_seconds=3 * args.tick_seconds,
                     takeover_after_seconds=args.tick_seconds * (args.ticks - 2),
                     external_lead_seconds=args.tick_seconds,
                     external_fallback_after_seconds=10**6)
        if args.anchor:
            cfg.anchors = tuple(args.anchor)
        prober = Prober(cfg)
        st = State()
        hb = {"none": (None, None), "running": (0.0, True), "stopped": (0.0, False)}[args.heartbeat]
        for i in range(args.ticks):
            o = Observation(now=time.time(), self_ok=prober.self_ok(), direct_ok=prober.direct_ok(),
                            heartbeat_age=hb[0], heartbeat_bot_running=hb[1],
                            standby_active=False, lease_mode=None)
            d = decide(o, st, cfg, args.armed)
            if d.action == "external_check":
                st.external_started_at = time.time()
                st.external_result = external_check(cfg) if args.external == "live" else args.external
                st.external_result_at = time.time()
                d = decide(o, st, cfg, args.armed)
            print(json.dumps({"tick": i, "self_ok": o.self_ok, "direct_ok": o.direct_ok,
                              "external": st.external_result, "decision": d.action, "reason": d.reason}), flush=True)
            if d.action == "takeover":
                print(json.dumps({"simulated": True, "actuated": False}))
                break
            time.sleep(args.tick_seconds)


if __name__ == "__main__":
    main()
