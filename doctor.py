#!/usr/bin/env python3
"""Maxwell install check: what works, what doesn't, and what to do about it.

    python3 doctor.py            # report
    python3 doctor.py --probe    # also call the model + embedding endpoints

Exits non-zero only when something actually stops the bot from starting —
missing optional features are reported, not treated as failures.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from importlib.util import find_spec
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent

GREEN, YELLOW, RED, DIM, BOLD, RESET = (
    ("\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)

problems: list[str] = []


def head(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")


def line(state: str, label: str, detail: str = "") -> None:
    mark = {"ok": f"{GREEN}✓{RESET}", "warn": f"{YELLOW}!{RESET}", "bad": f"{RED}✗{RESET}"}[
        state
    ]
    print(f"  {mark} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def check_python() -> None:
    head("Python")
    if sys.version_info >= (3, 11):
        line("ok", f"Python {sys.version.split()[0]}")
    else:
        line("bad", f"Python {sys.version.split()[0]}", "3.11+ required")
        problems.append("upgrade to Python 3.11 or newer")


def check_core_packages() -> None:
    head("Core packages")
    required = {
        "discord": "discord.py-self",
        "aiohttp": "aiohttp",
        "aiofiles": "aiofiles",
        "dotenv": "python-dotenv",
        "numpy": "numpy",
    }
    for module, package in required.items():
        if find_spec(module):
            line("ok", package)
        else:
            line("bad", package, f"pip install {package}")
            problems.append(f"pip install {package}")


def check_env_file() -> None:
    head("Configuration")
    env_file = Path(os.getenv("MAXWELL_ENV_FILE", APP_ROOT / ".env"))
    if env_file.is_file():
        line("ok", ".env found", str(env_file))
    else:
        line("bad", ".env missing", "run ./setup.sh, or cp .env.example .env")
        problems.append("create a .env (./setup.sh)")


def check_required_settings(cfg) -> None:
    if cfg is None:
        return
    if cfg.DISCORD_TOKEN:
        line("ok", "DISCORD_TOKEN set")
    else:
        line("bad", "DISCORD_TOKEN missing", "the bot cannot start without it")
        problems.append("set DISCORD_TOKEN in .env")
    if cfg.OLLAMA_BASE_URL and cfg.OLLAMA_MODEL:
        line("ok", "model endpoint", f"{cfg.OLLAMA_MODEL} @ {cfg.OLLAMA_BASE_URL}")
    else:
        line("bad", "model endpoint incomplete", "set OLLAMA_BASE_URL and OLLAMA_MODEL")
        problems.append("set OLLAMA_BASE_URL and OLLAMA_MODEL in .env")
    if cfg.MAXWELL_OWNER_IDS:
        line("ok", "MAXWELL_OWNER_IDS set", f"{len(cfg.MAXWELL_OWNER_IDS)} owner(s)")
    else:
        line("warn", "MAXWELL_OWNER_IDS empty", "admin commands will be denied to everyone")
    if cfg.MAXWELL_ADMIN_PASSWORD:
        line("ok", "dashboard password set")
    else:
        line("warn", "MAXWELL_ADMIN_PASSWORD empty", "the admin API will answer 503")


def check_system_tools() -> None:
    head("Optional system tools")
    tools = [
        ("ffmpeg", "video frames, TTS playback, audio conversion"),
        ("espeak-ng", "offline TTS voice"),
        ("yt-dlp", "the youtube tool"),
        ("node", "yt-dlp's YouTube JS challenge solver"),
    ]
    for binary, purpose in tools:
        # Same resolution the bot uses: PATH, then this interpreter's bin dir.
        from config import _has_binary

        if _has_binary(binary):
            line("ok", binary, purpose)
        else:
            line("warn", f"{binary} not found", f"needed for: {purpose}")


def check_docker(cfg) -> None:
    """The shell and sub-agent tools run in containers, so Docker is required.

    Nothing else reported this: `shell` and `sub_agent` would just fail at call
    time with a docker error the operator only saw in a Discord reply.
    """
    if cfg is None or not (
        getattr(cfg, "ENABLE_SHELL", False) or getattr(cfg, "ENABLE_SUBAGENT", False)
    ):
        return
    head("Docker (needed by the shell and sub-agent tools)")
    import shutil
    import subprocess

    if not shutil.which("docker"):
        line(
            "warn",
            "docker not found",
            "shell/sub_agent will fail; install Docker, set ENABLE_SHELL=false, "
            "or set SUBAGENT_SANDBOX=host to run sub-agent commands unsandboxed",
        )
        return
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        line("warn", "docker not usable", f"{type(e).__name__}: {e}")
        return
    if proc.returncode == 0:
        line("ok", "docker daemon reachable", f"server {proc.stdout.strip()}")
    else:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        line(
            "warn",
            "docker installed but not reachable",
            (detail[-1][:120] if detail else "is the daemon running, and are you in the docker group?"),
        )


def check_x(cfg) -> None:
    """What X can actually do here — reading, posting, or neither.

    ENABLE_X being on says almost nothing on its own: the read half works
    with no credentials, so the only real question is whether there is a
    session to post with. Answer it here rather than at the first failed
    x_post in a channel.
    """
    if cfg is None or not getattr(cfg, "ENABLE_X", False):
        return
    head("X (Twitter)")
    handle = getattr(cfg, "X_HANDLE", "")
    if getattr(cfg, "X_AUTH_TOKEN", "") and getattr(cfg, "X_CT0", ""):
        line("ok", "session cookies set", f"posts as @{handle}" if handle else "X_HANDLE unset")
    elif getattr(cfg, "X_API_BASE_URL", ""):
        line("ok", "gateway configured", getattr(cfg, "X_API_BASE_URL", ""))
    else:
        line(
            "warn",
            "read-only",
            "no X_AUTH_TOKEN/X_CT0 and no X_API_BASE_URL — x_post cannot post",
        )
    sources = ["syndication (no account)"] if getattr(cfg, "X_SYNDICATION", True) else []
    if getattr(cfg, "X_RSS_BASE_URL", ""):
        sources.append(f"rss ({cfg.X_RSS_BASE_URL})")
    if sources:
        line("ok", "public reads", ", ".join(sources))
    else:
        line("warn", "no credential-free read source", "set X_RSS_BASE_URL or X_SYNDICATION=true")
    if not handle:
        line("warn", "X_HANDLE unset", "mentions cannot be polled without it")


def check_features(cfg) -> None:
    if cfg is None:
        return
    head("Features")
    for _, label, enabled, reason in cfg.feature_report():
        line("ok" if enabled else "warn", label, reason)


async def _probe_chat(cfg) -> tuple[str, str]:
    import aiohttp

    from providers import normalize_base_url

    # Same URL the bot itself builds, so a green line here means the bot works.
    url = f"{normalize_base_url(cfg.OLLAMA_BASE_URL)}/models"
    headers = {"Authorization": f"Bearer {cfg.OLLAMA_API_KEY}"} if cfg.OLLAMA_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status < 400:
                    return "ok", f"HTTP {resp.status} from {url}"
                return "bad", f"HTTP {resp.status} from {url}"
    except Exception as e:
        return "bad", f"{type(e).__name__}: {e}"


async def _probe_embeddings(cfg) -> tuple[str, str]:
    import aiohttp

    from rag_memory import EMBED_HEADERS, EMBED_MODEL, EMBED_URL

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                EMBED_URL,
                json={"model": EMBED_MODEL, "input": "maxwell doctor probe"},
                headers=EMBED_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.text()
                if resp.status < 400:
                    return "ok", f"{EMBED_MODEL} @ {EMBED_URL}"
                return "warn", f"HTTP {resp.status} from {EMBED_URL}: {body[:120]}"
    except Exception as e:
        return "warn", f"{type(e).__name__}: {e}"


def probe(cfg) -> None:
    if cfg is None:
        return
    head("Live endpoint probe")
    state, detail = asyncio.run(_probe_chat(cfg))
    line(state, "chat endpoint", detail)
    if state == "bad":
        problems.append("the model endpoint is unreachable — check OLLAMA_BASE_URL/API key")
    if cfg.ENABLE_RAG:
        state, detail = asyncio.run(_probe_embeddings(cfg))
        line(state, "embedding endpoint", detail)
        if state != "ok":
            print(
                f"    {DIM}RAG memory degrades to recent-history context. Fix with "
                f"`ollama pull qwen3-embedding:0.6b`, point MAXWELL_EMBED_BASE_URL at "
                f"another endpoint, or set ENABLE_RAG=false.{RESET}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a Maxwell install.")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="also call the model and embedding endpoints",
    )
    args = parser.parse_args()

    print(f"{BOLD}Maxwell install check{RESET}  {DIM}{APP_ROOT}{RESET}")
    check_python()
    check_core_packages()
    check_env_file()

    cfg = None
    try:
        from config import Config

        cfg = Config
    except Exception as e:  # config itself failed to load — that's fatal
        line("bad", "config.py failed to load", str(e))
        problems.append(f"fix config loading: {e}")

    check_required_settings(cfg)
    check_system_tools()
    check_docker(cfg)
    check_x(cfg)
    check_features(cfg)
    if args.probe:
        probe(cfg)

    head("Summary")
    if problems:
        for item in problems:
            print(f"  {RED}→{RESET} {item}")
        return 1
    print(f"  {GREEN}Ready.{RESET} Start with: python3 bot.py")
    if not args.probe:
        print(f"  {DIM}Run `python3 doctor.py --probe` to test the endpoints too.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
