"""Exercise supervision with real local child processes, without Docker."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from docker.supervisor import supervise

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_child_exit_stops_and_reaps_its_sibling(tmp_path, exit_code):
    pid_file = tmp_path / "pid"
    # Wait for the sibling to start before exiting, avoiding timing races.
    exiting = [
        sys.executable,
        "-c",
        f"""
import time
from pathlib import Path
while not Path({str(pid_file)!r}).exists():
    time.sleep(0.01)
raise SystemExit({exit_code})
""",
    ]
    sibling = [
        sys.executable,
        "-c",
        f"""
import os, time
from pathlib import Path
Path({str(pid_file)!r}).write_text(str(os.getpid()))
time.sleep(30)
""",
    ]
    assert asyncio.run(asyncio.wait_for(supervise([exiting, sibling], 0.1), 5)) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_sigterm_stops_supervisor_and_uncooperative_child(tmp_path):
    pid_file = tmp_path / "pid"
    child = [
        sys.executable,
        "-c",
        f"""
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(pid_file)!r}).write_text(str(os.getpid()))
time.sleep(30)
""",
    ]
    code = f"import asyncio; from docker.supervisor import supervise; raise SystemExit(asyncio.run(supervise([{child!r}], 0.1)))"
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT)
    try:
        import time

        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
