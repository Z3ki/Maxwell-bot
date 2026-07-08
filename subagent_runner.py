"""OpenCode sub-agent runner for Maxwell Bot.

Spawns long-running opencode `run` tasks in isolated work directories, captures
stdout/stderr, and notifies the channel when the task finishes.
"""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import File
from io import BytesIO

logger = None  # initialized lazily via _ensure_logger


def _ensure_logger():
    global logger
    if logger is None:
        import logging

        logger = logging.getLogger(__name__)
    return logger


def _load_hermes_ollama_key() -> str | None:
    """Load OLLAMA_API_KEY from /root/.hermes/.env if present.

    Hermes stores a fingerprint but not the plaintext secret in auth.json; the
    actual key is sourced from its dedicated env file. This lets opencode use
    the same Ollama Cloud credential without exposing it in Maxwell config.
    """
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
                    return line[len("OLLAMA_API_KEY="):].strip().strip('"\'')
    except Exception:
        _ensure_logger().debug("Failed to read /root/.hermes/.env", exc_info=True)
    return None


def _default_opencode_bin() -> str:
    return shutil.which("opencode") or "/root/.opencode/bin/opencode"


def subagent_base_dir() -> Path:
    return Path(os.environ.get("OPENCODE_SUBAGENT_BASE_DIR", "subagents")).resolve()


def _workdir_for(slug: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())[:40].strip("-") or "task"
    return subagent_base_dir() / f"{safe_slug}-{ts}-{os.urandom(4).hex()}"


def _write_opencode_config(workdir: Path, model: str, timeout_ms: int) -> Path:
    """Write an isolated opencode.json config inside the work directory.

    The model string should be in provider/model form, e.g.
    "ollama-cloud/minimax-m3:cloud".
    """
    provider_id, _, model_id = model.partition("/")
    provider_id = provider_id or "ollama-cloud"
    model_id = model_id or "minimax-m3:cloud"

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{provider_id}/{model_id}",
        # OpenCode timeout is in milliseconds.
        "provider": {
            provider_id: {
                "options": {
                    "timeout": timeout_ms,
                    "chunkTimeout": 120000,
                }
            }
        },
        # Prefer a cheap small model for title/summary if available.
        "small_model": "opencode/mimo-v2.5-free",
        # Auto-approve non-destructive file tools so the subagent can run without
        # blocking on stdin. Destructive shell commands still require stdin in
        # auto mode unless the user has explicitly allowed them, but at least
        # editing files inside the workdir is not stuck.
        "permission": {
            "edit": "allow",
            "write": "allow",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "bash": "allow",
            "question": "deny",
        },
        "snapshot": True,
        "share": "disabled",
    }
    config_path = workdir / "opencode.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


async def _run_opencode(
    workdir: Path,
    prompt: str,
    *,
    model: str = "ollama-cloud/minimax-m3:cloud",
    timeout_minutes: int = 30,
    opencode_bin: str | None = None,
    extra_files: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run opencode and capture output."""
    opencode_bin = opencode_bin or _default_opencode_bin()
    if not Path(opencode_bin).exists():
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"opencode binary not found: {opencode_bin}",
        }

    timeout_ms = max(600000, timeout_minutes * 60 * 1000)
    config_path = _write_opencode_config(workdir, model, timeout_ms)

    cmd = [
        opencode_bin,
        "run",
        "--prompt",
        prompt,
        "--dir",
        str(workdir),
        "--auto",
    ]
    for fpath in extra_files or []:
        cmd.extend(["--file", fpath])

    run_env = dict(os.environ)
    run_env["OPENCODE_CONFIG"] = str(config_path)
    run_env["OPENCODE_CONFIG_DIR"] = str(workdir / ".opencode")
    run_env["OPENCODE_DISABLE_AUTOCOMPACT"] = "true"
    # Pull Ollama Cloud credential from the Hermes env file if not already set.
    if "OLLAMA_API_KEY" not in run_env or not run_env["OLLAMA_API_KEY"]:
        hermes_key = _load_hermes_ollama_key()
        if hermes_key:
            run_env["OLLAMA_API_KEY"] = hermes_key
    if env:
        run_env.update(env)

    _ensure_logger().info(
        "Starting OpenCode subagent in %s with model %s timeout %sms",
        workdir,
        model,
        timeout_ms,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workdir),
        env=run_env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_minutes * 60
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": "",
            "stderr": f"Sub-agent timed out after {timeout_minutes} minutes",
        }

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode or 0,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
    }


async def _post_result(
    channel,
    prompt: str,
    workdir: Path,
    result: dict[str, Any],
    *,
    sender_name: str = "Sub-agent",
    max_chars: int = 1900,
) -> None:
    """Post the sub-agent result back to the Discord channel."""
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = result.get("exit_code", -1)

    summary = f"**{sender_name} finished** — exit code `{exit_code}`\n"
    summary += f"Workdir: `{workdir}`\n"
    summary += f"Task: {prompt[:200]}{'...' if len(prompt) > 200 else ''}\n\n"

    body = ""
    if stdout.strip():
        body += f"**Output**\n```\n{stdout.strip()}\n```\n"
    if stderr.strip():
        body += f"**Errors**\n```\n{stderr.strip()}\n```\n"
    if not stdout.strip() and not stderr.strip():
        body += "_(no output)_\n"

    text = summary + body
    if len(text) <= max_chars:
        try:
            await channel.send(text)
        except discord.Forbidden:
            _ensure_logger().warning("Cannot post subagent result: missing permissions")
        return

    # Too long for chat — send summary + attach full output as a file.
    chunk = summary + "\nFull output attached.\n"
    try:
        await channel.send(
            chunk,
            file=File(
                BytesIO(text.encode("utf-8")),
                filename="subagent-output.txt",
            ),
        )
    except discord.Forbidden:
        _ensure_logger().warning("Cannot post subagent result: missing permissions")
    except Exception as e:
        _ensure_logger().error("Failed to post subagent result: %s", e)


async def run_subagent_task(
    bot,
    message,
    prompt: str,
    *,
    slug: str = "task",
    model: str | None = None,
    timeout_minutes: int = 30,
    extra_files: list[str] | None = None,
) -> str:
    """Kick off a background opencode task and schedule a channel notification.

    Returns the immediate string shown to the LLM so the main bot can keep
    responding to chat while the sub-agent works.
    """
    base = subagent_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    workdir = _workdir_for(slug)
    workdir.mkdir(parents=True, exist_ok=True)

    model = model or os.environ.get(
        "OPENCODE_SUBAGENT_MODEL", "ollama-cloud/minimax-m3:cloud"
    )
    opencode_bin = os.environ.get("OPENCODE_BIN", _default_opencode_bin())

    task = asyncio.create_task(
        _run_opencode(
            workdir,
            prompt,
            model=model,
            timeout_minutes=timeout_minutes,
            opencode_bin=opencode_bin,
            extra_files=extra_files,
        )
    )

    task_id = os.urandom(8).hex()
    tracker = getattr(bot, "_subagent_tasks", {})
    tracker[task_id] = {"task": task, "workdir": workdir, "prompt": prompt}
    bot._subagent_tasks = tracker

    channel = getattr(message, "channel", None)

    async def _on_complete(t):
        try:
            result = await t
        except Exception as e:
            result = {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Sub-agent crashed: {e}",
            }
        if channel is not None:
            try:
                await _post_result(channel, prompt, workdir, result)
            except Exception as e:
                _ensure_logger().error("Failed to notify channel about subagent: %s", e)
        tracker.pop(task_id, None)

    task.add_done_callback(lambda t: asyncio.create_task(_on_complete(t)))

    return (
        f"__SUBAGENT_STARTED__ {task_id}\n"
        f"Started OpenCode subagent in `{workdir}` using `{model}`.\n"
        f"It will run for up to {timeout_minutes} minutes in the background; "
        f"I'll post the result here when it finishes."
    )
