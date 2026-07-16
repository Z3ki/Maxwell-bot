"""Dockerized OpenCode sub-agent runner for Maxwell Bot.

Builds/runs a dedicated container per task, mirroring the security posture of the
existing maxwell-shell sandbox while giving OpenCode a writable workdir and
Node/opencode runtime.
"""

import asyncio
import contextlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

IMAGE_NAME = "maxwell-opencode"
DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker")
DOCKERFILE_PATH = os.path.join(DOCKERFILE_DIR, "opencode.Dockerfile")
CONTAINER_USER = "opencode"
CONTAINER_HOME = "/home/opencode"
CONTAINER_WORKDIR = "/home/opencode"

# OpenCode provider timeout is in milliseconds; match host runner default.
DEFAULT_TIMEOUT_MS = 600000
DEFAULT_MEMORY = "2g"
DEFAULT_CPUS = "1.0"
# Default to 'bridge' so the container can reach the operator-configured
# LLM endpoint. 'none' severs DNS resolution entirely and would break
# every provider call. Operators who want a sealed sandbox can set
# OPENCODE_SUBAGENT_NETWORK=none explicitly — but they must also expose
# the LLM endpoint through a sidecar, host gateway, or pre-baked hosts
# entries for this to be useful.
DEFAULT_NETWORK = "bridge"


logger = None  # initialized lazily via _ensure_logger


def _ensure_logger():
    global logger
    if logger is None:
        import logging

        logger = logging.getLogger(__name__)
    return logger


def _load_hermes_ollama_key() -> str | None:
    """Load OLLAMA_API_KEY from /root/.hermes/.env if present."""
    env_path = Path("/root/.hermes/.env")
    if not env_path.is_file():
        return None
    try:
        with env_path.open() as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("OLLAMA_API_KEY="):
                    return line[len("OLLAMA_API_KEY=") :].strip().strip("\"'")
    except Exception:
        _ensure_logger().debug("Failed to read /root/.hermes/.env", exc_info=True)
    return None


def _write_opencode_config(workdir: Path, model: str, timeout_ms: int) -> Path:
    """Write an isolated opencode.json config inside the work directory."""
    provider_id, _, model_id = model.partition("/")
    provider_id = provider_id or "ollama-cloud"
    model_id = model_id or "minimax-m3"

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{provider_id}/{model_id}",
        "provider": {
            provider_id: {
                "options": {
                    "timeout": timeout_ms,
                    "chunkTimeout": 120000,
                }
            }
        },
        "small_model": "opencode/mimo-v2.5-free",
        "permission": {"*": "allow"},
        "snapshot": True,
        "share": "disabled",
    }
    config_path = workdir / "opencode.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


def _default_opencode_bin() -> str:
    return "opencode"


async def _run_docker(*args: str, timeout: int = 30) -> tuple[bytes, bytes, int]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as _exc:
        proc.kill()
        await proc.wait()
        raise
    return stdout, stderr, proc.returncode or 0


async def ensure_image() -> None:
    """Build the maxwell-opencode image if it does not already exist."""
    stdout, _stderr, code = await _run_docker(
        "inspect", "--type", "image", f"--format={IMAGE_NAME}", IMAGE_NAME, timeout=15
    )
    if code == 0 and stdout.decode(errors="replace").strip() == IMAGE_NAME:
        return

    _ensure_logger().info("Building %s image from %s", IMAGE_NAME, DOCKERFILE_PATH)
    _stdout, stderr, build_code = await _run_docker(
        "build",
        "-f",
        DOCKERFILE_PATH,
        "-t",
        IMAGE_NAME,
        DOCKERFILE_DIR,
        timeout=600,
    )
    if build_code != 0:
        raise RuntimeError(
            stderr.decode(errors="replace").strip() or "docker build failed"
        )


async def _resolve_container_uid() -> int:
    """Return the numeric UID of CONTAINER_USER inside the maxwell-opencode image.

    The Dockerfile creates the user via `useradd -m -s /bin/bash opencode`, so the
    UID is whatever the image build assigned (typically 1000, but could be higher
    on hosts with a large UID range already taken). Resolving it dynamically means
    the chown call below works regardless of host UID range.
    """
    stdout, _stderr, code = await _run_docker(
        "run",
        "--rm",
        "--entrypoint",
        "id",
        IMAGE_NAME,
        "-u",
        CONTAINER_USER,
        timeout=15,
    )
    if code != 0:
        _ensure_logger().warning(
            "Could not resolve container UID, falling back to 1000: %s",
            _stderr.decode(errors="replace").strip() if _stderr else "",
        )
        return 1000
    # `id -u opencode` prints a single number.
    try:
        return int(stdout.decode(errors="replace").strip().split()[0])
    except (ValueError, IndexError):
        return 1000


async def _chown_workdir_to_container(workdir: Path, uid: int) -> None:
    """Best-effort chown of workdir (and any existing children) to the container UID.

    Only fixes ownership when the host UID is different from the target. We avoid
    recursing on the entire tree to keep large workdirs cheap; the opencode runtime
    only needs to write a handful of config/cache files at the top level.
    """
    try:
        st = workdir.stat()
    except OSError:
        return
    current_uid = st.st_uid
    if current_uid == uid:
        return
    # Top-level chown (no -R) so we don't pay to walk megabytes of git history.
    # If children exist with wrong ownership, the runtime will create the
    # .opencode / .cache subdirs itself (they'll inherit the new owner).
    try:
        os.chown(workdir, uid, uid)
    except PermissionError:
        _ensure_logger().warning(
            "Could not chown %s to UID %s (permission denied); container writes may fail",
            workdir,
            uid,
        )
    except OSError as e:
        _ensure_logger().warning("chown %s -> %s failed: %s", workdir, uid, e)


async def run_opencode_in_docker(
    workdir: Path,
    prompt: str,
    *,
    model: str = "ollama-cloud/minimax-m3",
    timeout_minutes: int = 30,
    extra_files: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run opencode inside a fresh, locked-down Docker container.

    Mirrors the maxwell-shell security posture with a writable workdir mount and
    injected OLLAMA_API_KEY.
    """
    await ensure_image()

    timeout_ms = max(DEFAULT_TIMEOUT_MS, timeout_minutes * 60 * 1000)
    config_path = _write_opencode_config(workdir, model, timeout_ms)

    memory = os.environ.get("OPENCODE_SUBAGENT_MEMORY", DEFAULT_MEMORY)
    cpus = os.environ.get("OPENCODE_SUBAGENT_CPUS", DEFAULT_CPUS)
    network = os.environ.get("OPENCODE_SUBAGENT_NETWORK", DEFAULT_NETWORK)

    container_name = f"maxwell-opencode-{os.urandom(8).hex()}"

    # The container runs as the 'opencode' user (UID is determined by the image,
    # usually 1000). If the host workdir is owned by root (e.g. PM2 under root,
    # or any operator-owned path), the container can't write opencode.json or
    # create .opencode/.cache and crashes. Resolve the in-image UID and chown
    # before launching so this works regardless of host UID.
    container_uid = await _resolve_container_uid()
    await _chown_workdir_to_container(workdir, container_uid)

    # Prepare per-container tmpfs and env; do not mount host /tmp.
    run_env = {
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_CONFIG_DIR": str(workdir / ".opencode"),
        "OPENCODE_DISABLE_AUTOCOMPACT": "true",
        "HOME": CONTAINER_HOME,
        "XDG_CONFIG_HOME": str(workdir / ".config"),
        "XDG_CACHE_HOME": str(workdir / ".cache"),
        "XDG_DATA_HOME": str(workdir / ".local" / "share"),
        "XDG_STATE_HOME": str(workdir / ".local" / "state"),
    }
    hermes_key = _load_hermes_ollama_key()
    if hermes_key:
        run_env["OLLAMA_API_KEY"] = hermes_key
    elif "OLLAMA_API_KEY" in os.environ and os.environ["OLLAMA_API_KEY"]:
        run_env["OLLAMA_API_KEY"] = os.environ["OLLAMA_API_KEY"]
    if env:
        run_env.update(env)

    # Build docker run command carefully to avoid shell injection.
    cmd = [
        "run",
        "--rm",
        "--read-only",
        "--network",
        network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        memory,
        "--cpus",
        cpus,
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "-v",
        f"{workdir}:{CONTAINER_HOME}:rw",
        "--user",
        CONTAINER_USER,
        "--name",
        container_name,
        "--workdir",
        CONTAINER_WORKDIR,
    ]
    for key, value in run_env.items():
        cmd.extend(["-e", f"{key}={value}"])

    opencode_bin = os.environ.get("OPENCODE_BIN", _default_opencode_bin())
    exec_cmd = [
        opencode_bin,
        "run",
        "--model",
        model,
        "--dir",
        CONTAINER_WORKDIR,
    ]
    for fpath in extra_files or []:
        # Mount points inside the container must be absolute; extra_files are
        # expected to be paths on the host. Copy them into the workdir so the
        # read-only root filesystem doesn't need extra mounts.
        src = Path(fpath)
        if src.is_file():
            dest = workdir / src.name
            if not dest.exists():
                try:
                    shutil.copy2(src, dest)
                except Exception:
                    _ensure_logger().warning("Could not copy extra file %s", fpath)
            exec_cmd.extend(["--file", str(Path(CONTAINER_HOME) / src.name)])
    exec_cmd.append(prompt)

    cmd.extend([IMAGE_NAME, *exec_cmd])

    _ensure_logger().info(
        "Starting OpenCode subagent container %s in %s with model %s timeout %sms",
        container_name,
        workdir,
        model,
        timeout_ms,
    )

    try:
        stdout, stderr, exit_code = await _run_docker(
            *cmd, timeout=timeout_minutes * 60
        )
    except asyncio.TimeoutError as _exc:
        # Best-effort cleanup; don't block result on it.
        with contextlib.suppress(Exception):
            await _run_docker("rm", "-f", container_name, timeout=15)
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": "",
            "stderr": f"Sub-agent timed out after {timeout_minutes} minutes",
        }

    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
    }
