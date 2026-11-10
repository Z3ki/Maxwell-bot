"""Primary-side half of guarded automatic failover. Standard library only.

* ``startup_gate(data_dir)`` runs inside the primary bot before Discord login.
  If the automatic-failover client is configured it asks the backup to hand
  back and waits (bounded) until the backup confirms its gateway is stopped.
  An unreachable backup never blocks the primary for long: the primary is the
  authority and the backup also steps down on its own when it sees the primary.
* ``python -m standby.primary_gate guard`` is a host service on the primary.
  Every few seconds it reports whether the primary bot container is running and,
  if the backup reports an active lease while the primary bot runs, it
  immediately requests handback (closing any overlap window).

The client pins the backup's exact TLS leaf certificate and IP SAN, uses a
dedicated failover-only key and never follows redirects or proxies.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import logging
import os
import socket
import ssl
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("maxwell.standby.gate")

GATE_DEADLINE = 150.0
CONNECT_TIMEOUT = 6.0
UNREACHABLE_ATTEMPTS = 2


class GateError(RuntimeError):
    pass


class BackupUnreachable(OSError):
    """TCP connect failed: the backup host/port is not reachable at all."""


class _PinnedConnection(http.client.HTTPSConnection):
    def connect(self):
        try:
            raw = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as error:
            raise BackupUnreachable(str(type(error).__name__)) from None
        # TCP works: anything failing from here on (TLS EOF/reset, busy) means the
        # backup is ALIVE and has not confirmed handback; never "unreachable".
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # type: ignore[attr-defined]
        except Exception:
            raw.close()
            raise GateError("backup reachable but TLS not completed") from None


def _private(path: Path) -> str:
    """Private regular file only: no symlink, FIFO or device can block the gate."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
        raise GateError("failover client file must be a private regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_mode & 0o077:
            raise GateError("failover client file must be a private regular file")
        return os.read(fd, 65537).decode()
    finally:
        os.close(fd)


class FailoverClient:
    def __init__(self, directory: Path):
        config = json.loads(_private(directory / "client.json"))
        host, port = config.get("host"), config.get("port")
        if not isinstance(host, str) or not isinstance(port, int) or not 0 < port < 65536:
            raise GateError("invalid failover client config")
        self.host, self.port = host, port
        self.key = _private(directory / "key").strip()
        self.cert_pem = _private(directory / "server.crt")
        self.expected = ssl.PEM_cert_to_DER_cert(self.cert_pem)
        if not self.key:
            raise GateError("missing failover key")

    def call(self, op: str, bot_running: bool, timeout: float = 45.0) -> dict[str, Any]:
        context = ssl.create_default_context(cadata=self.cert_pem)
        # Short connect/TLS timeout so an unreachable backup is detected fast;
        # the longer timeout only applies to waiting for a confirmed stop.
        connection = _PinnedConnection(
            self.host, self.port, context=context, timeout=min(CONNECT_TIMEOUT, timeout)
        )
        try:
            connection.connect()
            sock = connection.sock
            if sock is not None:
                sock.settimeout(timeout)
            if sock is None or not hmac.compare_digest(sock.getpeercert(binary_form=True) or b"", self.expected):
                raise GateError("backup TLS pin mismatch")
            body = json.dumps({"op": op, "bot_running": bot_running}).encode()
            connection.request("POST", "/failover", body=body, headers={
                "Content-Type": "application/json", "X-Failover-Key": self.key})
            response = connection.getresponse()
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096:
                raise GateError("backup rejected failover call")
            answer = json.loads(raw)
            if not isinstance(answer, dict) or answer.get("ok") is not True:
                raise GateError("unexpected failover answer")
            return answer
        finally:
            connection.close()


def startup_gate(
    data_dir: str | Path,
    *,
    client_factory: Callable[[Path], Any] = FailoverClient,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline: float = GATE_DEADLINE,
) -> str:
    """Return a short outcome. NEVER raises: a broken gate must not stop main."""
    directory = Path(data_dir) / "standby-failover"
    started = clock()
    try:
        if not (directory / "client.json").exists():
            return "not configured"
        client = client_factory(directory)
    except Exception as error:
        logger.error("Standby failover gate misconfigured (%s); starting", type(error).__name__)
        return "misconfigured"
    failures = 0
    answered = False
    while True:
        remaining = deadline - (clock() - started)
        if remaining <= 0:
            logger.error("Standby failover gate deadline reached; primary proceeds")
            return "deadline; primary proceeds"
        try:
            answer = client.call("handback", True, timeout=max(1.0, min(45.0, remaining)))
            answered = True
            if answer.get("standby_active") is False and answer.get("stepped_down") is True:
                logger.info("Standby failover gate: backup confirmed inactive")
                return "backup inactive"
            logger.warning("Standby failover gate: backup still active; waiting")
        except BackupUnreachable as error:
            failures += 1
            logger.warning("Standby failover gate: backup unreachable (%s)", type(error).__name__)
            if not answered and failures >= UNREACHABLE_ATTEMPTS:
                return "backup unreachable; primary proceeds (backup self-demotes)"
        except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException, GateError, ValueError) as error:
            # Reachable but no confirmation: keep waiting (bounded by the deadline).
            answered = True
            logger.warning("Standby failover gate: backup did not confirm (%s); waiting", type(error).__name__)
        except Exception as error:  # pragma: no cover - defensive
            logger.error("Standby failover gate error (%s); starting", type(error).__name__)
            return "error"
        sleep(3)


def container_bot_running(runner=subprocess.run, container: str = "maxwell") -> bool:
    """Running container whose healthcheck (bot.py process present) is not failing."""
    out = runner(
        ["docker", "inspect", "--format", "{{json .State}}", container],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        return False
    state = json.loads(out.stdout)
    health = (state.get("Health") or {}).get("Status")
    return bool(state.get("Running")) and health in {None, "healthy", "starting"}


def guard_tick(client: Any, *, bot_running: Callable[[], bool]) -> dict[str, Any]:
    running = bot_running()
    answer = client.call("heartbeat", running, timeout=10)
    outcome = {"at": time.time(), "bot_running": running, "backup": answer, "handback": None}
    if running and answer.get("standby_active") and answer.get("lease_mode") == "automatic":
        outcome["handback"] = client.call("handback", True, timeout=45)
    return outcome


def guard(directory: Path, status: Path, interval: float = 15.0) -> None:
    client = FailoverClient(directory)
    while True:
        started = time.monotonic()
        try:
            outcome = guard_tick(client, bot_running=container_bot_running)
        except Exception as error:
            outcome = {"at": time.time(), "error": type(error).__name__}
        print(json.dumps(outcome), flush=True)
        try:
            tmp = status.with_suffix(".tmp")
            tmp.write_text(json.dumps(outcome))
            os.replace(tmp, status)
        except OSError:
            pass
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role", required=True)
    g = sub.add_parser("guard")
    g.add_argument("--client-dir", type=Path, default=Path("/etc/maxwell-failover-client"))
    g.add_argument("--status", type=Path, default=Path("/run/maxwell-primary-guard/status.json"))
    s = sub.add_parser("status")
    s.add_argument("--client-dir", type=Path, default=Path("/etc/maxwell-failover-client"))
    t = sub.add_parser("gate-test", help="run the startup gate exactly as bot.py does")
    t.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.role == "guard":
        guard(args.client_dir, args.status)
    elif args.role == "status":
        print(json.dumps(FailoverClient(args.client_dir).call("status", container_bot_running(), timeout=10)))
    else:
        print(startup_gate(args.data_dir))


if __name__ == "__main__":
    main()
