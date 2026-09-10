"""Main-bot self-heal: catch tool crashes, patch this checkout, open a PR.

Unhandled TypeError/AttributeError/… from a tool handler used to die in the
turn and sit in logs. This module, called from ``_execute_tool_by_name``,
packages the traceback, asks Maxwell's *own* chat model (not a sub-agent)
for a minimal patch + unit test, applies it on an isolated git worktree, and
opens a GitHub PR for a human to review.

Constraints, all enforced in code:
- never check out, merge, or push ``main`` / ``master`` / other protected names
- never auto-merge
- never run a sub-agent / spawn_background worker
- skip under pytest, when disabled, and when the same crash already has a PR
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import traceback
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from utils import _spawn_background

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent

# Positional-only dispatcher params that must never leak into tool.execute.
EXECUTE_RESERVED_KEYS = frozenset({"self", "message"})

PROTECTED_BRANCHES = frozenset(
    {"main", "master", "develop", "trunk", "production", "prod", "release"}
)

# Programming mistakes. Timeouts, HTTP, and disk errors are not code bugs.
AUTOFIXABLE_TYPES = (
    TypeError,
    AttributeError,
    KeyError,
    NameError,
    IndexError,
    AssertionError,
    UnboundLocalError,
    ZeroDivisionError,
    RecursionError,
)

_SECRET_KEY_RE = re.compile(
    r"(?i)^(?:[a-z0-9]+[_-])*(api[_-]?key|token|password|secret|authorization|cookie|session|"
    r"passwd|private[_-]?key|access[_-]?token|refresh[_-]?token|bearer)$"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|password|secret)[\"']?\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|\S+))"
)

_BLOCKED_PATH_PARTS = (
    ".git",
    ".env",
    "data",
    "data_gf",
    "logs",
    "node_modules",
    ".venv",
    "venv",
    "shelldocker",
    "subagents",
)
_BLOCKED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".env")
_ALLOWED_SUFFIXES = (".py",)

MAX_FILES_PER_PATCH = 8
MAX_FILE_CHARS = 120_000
MAX_CONTEXT_CHARS = 80_000
MAX_TRACE_CHARS = 12_000
COOLDOWN_DEFAULT_HOURS = 24
MAX_PER_HOUR_DEFAULT = 3

_IN_AUTOFIX: ContextVar[bool] = ContextVar("maxwell_autofix", default=False)
_schedule_lock = asyncio.Lock()
_active_fingerprints: set[str] = set()


def env_forced_off() -> bool:
    raw = str(os.getenv("MAXWELL_AUTOFIX", "") or "").strip().lower()
    return raw in {"0", "false", "no", "off"}


def is_under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def redact_diagnostics(text: str) -> str:
    """Keep configured credentials out of model prompts and public crash reports."""
    for key, value in os.environ.items():
        if _SECRET_KEY_RE.match(key) and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    return _SECRET_VALUE_RE.sub("[redacted]", text)


def sanitize_tool_args(value: Any, *, _depth: int = 0) -> Any:
    """Drop secrets and trim bulky bodies before they hit a prompt or a PR."""
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_val in list(value.items())[:40]:
            key = str(raw_key)
            if _SECRET_KEY_RE.match(key) or key.startswith("_"):
                out[key] = "[redacted]"
            else:
                out[key] = sanitize_tool_args(raw_val, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_tool_args(v, _depth=_depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        text = redact_diagnostics(value)
        limit = 200 if _depth == 0 else 400
        if len(text) > limit:
            return text[:limit] + f"…[{len(value)} chars]"
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_tool_args(str(value), _depth=_depth + 1)


def fingerprint_error(tool_name: str, exc: BaseException, tb_text: str) -> str:
    last_frame = ""
    for line in reversed(str(tb_text or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("File ") and str(REPO_ROOT) in stripped:
            last_frame = stripped
            break
        if stripped.startswith("File ") and last_frame == "":
            last_frame = stripped
    payload = f"{tool_name}|{type(exc).__name__}|{last_frame}|{type(exc).__name__}: {exc}"
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def is_autofixable(exc: BaseException) -> bool:
    if isinstance(exc, AUTOFIXABLE_TYPES):
        return True
    # The historical dispatcher collision is a TypeError; keep the message
    # match so a subclass or wrap still qualifies.
    text = f"{type(exc).__name__}: {exc}"
    return "multiple values for argument" in text


def traceback_mentions_autofix(tb_text: str) -> bool:
    return "autofix.py" in str(tb_text or "")


def branch_name(error_type: str, when: float | None = None) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(when or time.time()))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(error_type or "Error")).strip("-")[:32]
    slug = slug or "Error"
    return f"fix/autofix-{slug}-{stamp}"


def is_protected_branch(name: str) -> bool:
    short = str(name or "").strip().removeprefix("refs/heads/")
    parts = [p for p in re.split(r"[/:]", short) if p]
    return any(p in PROTECTED_BRANCHES for p in parts)


def allowed_relpath(rel: str, *, repo: Path = REPO_ROOT) -> Path | None:
    raw = str(rel or "").strip().replace("\\", "/")
    if not raw or raw.startswith(("/", "~")):
        return None
    if ".." in Path(raw).parts:
        return None
    lowered = raw.lower()
    if lowered.endswith(_BLOCKED_SUFFIXES) or lowered.startswith(".env"):
        return None
    parts = Path(raw).parts
    if any(p in _BLOCKED_PATH_PARTS for p in parts):
        return None
    if not raw.endswith(_ALLOWED_SUFFIXES):
        return None
    candidate = (repo / raw).resolve()
    try:
        resolved_rel = candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    if resolved_rel.as_posix().lower().startswith(".env"):
        return None
    if any(part in _BLOCKED_PATH_PARTS for part in resolved_rel.parts):
        return None
    if not candidate.name.endswith(_ALLOWED_SUFFIXES):
        return None
    return candidate


def parse_llm_patch(text: str) -> dict[str, Any]:
    """Accept a JSON object, optionally wrapped in a fenced code block."""
    blob = str(text or "").strip()
    if not blob:
        raise ValueError("empty model response")
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", blob, re.DOTALL)
    if fence:
        blob = fence.group(1)
    else:
        start, end = blob.find("{"), blob.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in model response")
        blob = blob[start : end + 1]
    data = json.loads(blob)
    if not isinstance(data, dict):
        raise TypeError("patch JSON must be an object")
    edits = data.get("edits") or []
    new_files = data.get("new_files") or data.get("files") or []
    if not isinstance(edits, list):
        edits = []
    if not isinstance(new_files, list):
        new_files = []
    return {
        "summary": str(data.get("summary") or "")[:500],
        "edits": [e for e in edits if isinstance(e, dict)],
        "new_files": [f for f in new_files if isinstance(f, dict)],
        "commit_message": str(
            data.get("commit_message") or "fix: autofix tool handler crash"
        )[:200],
        "pr_title": str(data.get("pr_title") or data.get("commit_message") or "fix: tool crash")[
            :120
        ],
        "pr_body": str(data.get("pr_body") or data.get("summary") or "")[:4000],
    }


def apply_patch(patch: dict[str, Any], *, root: Path) -> list[str]:
    """Apply search/replace edits and new files under ``root``. Returns relpaths."""
    changed: list[str] = []
    total = 0
    items = list(patch.get("edits") or []) + list(patch.get("new_files") or [])
    if len(items) > MAX_FILES_PER_PATCH:
        raise ValueError(f"patch touches {len(items)} files; cap is {MAX_FILES_PER_PATCH}")

    for edit in patch.get("edits") or []:
        rel = str(edit.get("path") or "").strip()
        dest = allowed_relpath(rel, repo=root)
        if dest is None:
            raise ValueError(f"blocked edit path: {rel!r}")
        old = edit.get("old")
        new = edit.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise TypeError(f"edit {rel} needs string old/new")
        if not dest.is_file():
            raise ValueError(f"edit target missing: {rel}")
        original = dest.read_text(encoding="utf-8")
        if old not in original:
            raise ValueError(f"search text not found in {rel}")
        updated = original.replace(old, new, 1)
        if len(updated) > MAX_FILE_CHARS:
            raise ValueError(f"{rel} would exceed {MAX_FILE_CHARS} chars")
        dest.write_text(updated, encoding="utf-8")
        total += len(updated)
        if rel not in changed:
            changed.append(rel)

    for spec in patch.get("new_files") or []:
        rel = str(spec.get("path") or "").strip()
        dest = allowed_relpath(rel, repo=root)
        if dest is None:
            raise ValueError(f"blocked new-file path: {rel!r}")
        content = spec.get("content")
        if not isinstance(content, str):
            raise TypeError(f"new file {rel} needs string content")
        if len(content) > MAX_FILE_CHARS:
            raise ValueError(f"{rel} exceeds {MAX_FILE_CHARS} chars")
        # New files are for tests reproducing the bug. Refuse overwriting
        # existing production modules so a sloppy patch cannot blank bot.py.
        if dest.exists():
            raise ValueError(f"refusing to overwrite existing {rel}")
        if dest.relative_to(root.resolve()).parts[0] != "tests":
            raise ValueError(f"new files must be under tests/: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        total += len(content)
        if rel not in changed:
            changed.append(rel)

    if total > MAX_FILE_CHARS * 2:
        raise ValueError("patch is too large")
    if not changed:
        raise ValueError("patch did not change any files")
    return changed


def _state_path(bot: Any) -> Path:
    data_dir = getattr(getattr(bot, "config", None), "DATA_DIR", None) or "data"
    path = Path(str(data_dir))
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path / "autofix_state.json"


def _load_state(bot: Any) -> dict[str, Any]:
    path = _state_path(bot)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"fingerprints": {}, "recent": []}


def _save_state(bot: Any, state: dict[str, Any]) -> None:
    path = _state_path(bot)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _control_flag(bot: Any, key: str, default: Any) -> Any:
    control = getattr(bot, "_control", None) or {}
    return control.get(key, default)


def should_schedule(bot: Any, tool_name: str, exc: BaseException, tb_text: str) -> str | None:
    """Return a fingerprint to run, or None if this crash should be ignored."""
    if env_forced_off() or is_under_pytest() or _IN_AUTOFIX.get():
        return None
    if not is_autofixable(exc):
        return None
    if traceback_mentions_autofix(tb_text):
        return None
    if not _control_flag(bot, "autofix_enabled", True):
        return None
    if getattr(bot, "ai_provider", None) is None and not callable(
        getattr(bot, "_generate_response", None)
    ):
        return None
    fp = fingerprint_error(tool_name, exc, tb_text)
    now = time.time()
    state = _load_state(bot)
    fingerprints = state.get("fingerprints") or {}
    cooldown_h = int(_control_flag(bot, "autofix_cooldown_hours", COOLDOWN_DEFAULT_HOURS) or 0)
    prev = fingerprints.get(fp) or {}
    try:
        prev_ts = float(prev.get("ts") or 0)
    except (TypeError, ValueError):
        prev_ts = 0.0
    if prev_ts and now - prev_ts < max(1, cooldown_h) * 3600:
        return None
    recent = [float(t) for t in (state.get("recent") or []) if t]
    hour_ago = now - 3600
    recent = [t for t in recent if t >= hour_ago]
    cap = int(_control_flag(bot, "autofix_max_per_hour", MAX_PER_HOUR_DEFAULT) or 0)
    cap = max(1, min(cap, 20))
    if len(recent) >= cap:
        return None
    if fp in _active_fingerprints:
        return None
    return fp


def gather_context(tb_text: str, *, repo: Path = REPO_ROOT) -> str:
    """Read repo files named in the traceback, windowed around the line."""
    chunks: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'File "([^"]+)", line (\d+)', str(tb_text or "")
    ):
        raw_path, line_s = match.group(1), match.group(2)
        path = Path(raw_path)
        try:
            resolved = path.resolve()
            resolved.relative_to(repo.resolve())
        except (ValueError, OSError):
            continue
        rel = str(resolved.relative_to(repo.resolve()))
        if rel in seen or allowed_relpath(rel, repo=repo) is None:
            continue
        seen.add(rel)
        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        lineno = max(1, int(line_s))
        if len(lines) <= 400:
            body = "\n".join(f"{i + 1:4d}|{row}" for i, row in enumerate(lines))
        else:
            start = max(0, lineno - 80)
            end = min(len(lines), lineno + 80)
            body = "\n".join(
                f"{i + 1:4d}|{lines[i]}" for i in range(start, end)
            )
        chunks.append(f"### {rel}:{lineno}\n```\n{body}\n```")
        if sum(len(c) for c in chunks) > MAX_CONTEXT_CHARS:
            break
    return "\n\n".join(chunks)


def build_prompt(
    *,
    tool_name: str,
    tool_args: Any,
    tb_text: str,
    context: str,
) -> list[dict[str, str]]:
    args_json = json.dumps(sanitize_tool_args(tool_args), ensure_ascii=False, indent=2)
    system = (
        "You are Maxwell fixing your own Python Discord-bot codebase. "
        "A tool handler crashed. Produce the smallest patch that fixes the "
        "root cause plus a pytest that reproduces it.\n"
        "Reply with ONE JSON object, no extra commentary, of the form:\n"
        "{\n"
        '  "summary": "one paragraph",\n'
        '  "edits": [{"path": "relative/file.py", "old": "exact text", "new": "replacement"}],\n'
        '  "new_files": [{"path": "tests/test_something.py", "content": "full file"}],\n'
        '  "commit_message": "fix: ...",\n'
        '  "pr_title": "fix: ...",\n'
        '  "pr_body": "markdown for reviewers"\n'
        "}\n"
        "Rules:\n"
        "- `old` must match the file exactly, including whitespace; one replacement each.\n"
        "- Prefer edits[] over rewriting a whole existing file.\n"
        "- New files only under tests/, and they must not need the network.\n"
        "- Do not touch .env, data/, secrets, or git config.\n"
        "- Do not add dependencies.\n"
        "- Do not spawn sub-agents; you ARE the fixer.\n"
    )
    user = (
        f"Tool: {tool_name}\n"
        f"Sanitized arguments:\n{args_json}\n\n"
        f"Traceback:\n```\n{tb_text[:MAX_TRACE_CHARS]}\n```\n\n"
        f"Source:\n{context or '(no in-repo frames found)'}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": redact_diagnostics(user)},
    ]


def _git(args: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run git. Refuses to push or check out a protected branch."""
    if not args:
        raise ValueError("empty git command")
    verb = args[0]
    joined = args[1:]
    if verb == "push":
        if "--force" in joined or "-f" in joined:
            raise RuntimeError("refusing force-push from autofix")
        for token in joined:
            if token.startswith("-") or token in {"origin", "HEAD"}:
                continue
            dest = token.split(":")[-1].removeprefix("refs/heads/")
            if is_protected_branch(dest):
                raise RuntimeError(f"refusing to push to protected branch {dest!r}")
    if verb in {"checkout", "switch", "merge", "rebase", "reset"}:
        for token in joined:
            if token.startswith("-"):
                continue
            if is_protected_branch(token):
                raise RuntimeError(f"refusing git {verb} of protected branch {token!r}")
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:800]
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return result


def current_branch(cwd: Path) -> str:
    out = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return (out.stdout or "").strip()


def origin_github_repo(cwd: Path) -> tuple[str, str] | None:
    try:
        out = _git(["remote", "get-url", "origin"], cwd=cwd)
    except RuntimeError:
        return None
    url = (out.stdout or "").strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        return None
    return match.group(1), match.group(2)


def _worktree_base(bot: Any) -> Path:
    data_dir = getattr(getattr(bot, "config", None), "DATA_DIR", None) or "data"
    path = Path(str(data_dir))
    if not path.is_absolute():
        path = REPO_ROOT / path
    dest = path / "autofix" / "worktrees"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


async def _generate_patch(bot: Any, messages: list[dict[str, str]]) -> str:
    generate = getattr(bot, "_generate_response", None)
    if not callable(generate):
        raise TypeError("bot has no _generate_response")
    acquire = getattr(bot, "_acquire_ai_slot", None)
    release = getattr(bot, "_release_ai_slot", None)
    if callable(acquire):
        await acquire(timeout=120, priority="background", key="autofix")
    try:
        response = await generate(
            messages,
            timeout=180,
            max_tokens=8192,
            tools=None,
            disable_reasoning=False,
        )
    finally:
        if callable(release):
            try:
                await release()
            except Exception:
                logger.debug("autofix slot release failed", exc_info=True)
    return str(response or "")


async def _run_pytest(worktree: Path, rel_tests: list[str]) -> str:
    from bot_tools import _run_docker_cmd
    from utils import docker_bind_path

    tests = [p for p in rel_tests if p.startswith("tests/") and p.endswith(".py")]
    if not tests:
        raise RuntimeError("autofix requires a regression test before publishing")
    if any(allowed_relpath(p, repo=worktree) is None for p in tests):
        raise RuntimeError("autofix test path is not allowed")
    image = os.getenv("MAXWELL_AUTOFIX_TEST_IMAGE", "").strip() or "maxwell:local"
    import uuid

    name = f"maxwell-autofix-test-{uuid.uuid4().hex}"
    # Test imports execute model-written code. Copy only the worktree into an
    # ephemeral, unprivileged container, never the live checkout or its secrets.
    runner = (
        "import os, shutil, sys; "
        "shutil.copytree('/source', '/tmp/work', "
        "ignore=shutil.ignore_patterns('.git', '.env*', '.venv', 'data', 'logs')); "
        "os.chdir('/tmp/work'); "
        "os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[1:]])"
    )
    try:
        (stdout, stderr), code = await _run_docker_cmd(
            "run", "--rm", "--pull=never", "--name", name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", "65534:65534",
            "--pids-limit", "128", "--memory", "1g", "--cpus", "1",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--mount", f"type=bind,src={docker_bind_path(worktree)},dst=/source,readonly",
            "-e", "HOME=/tmp", "-e", "DATA_DIR=/tmp/data",
            "-e", "MAXWELL_ENV_FILE=/dev/null", "-e", "MAXWELL_AUTOFIX=false",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint", "python3", image, "-c", runner,
            *tests, "-q", "--tb=short", "-o", "cache_dir=/tmp/pytest-cache",
            timeout=120, output_limit=64_000,
        )
    except asyncio.TimeoutError:
        raise RuntimeError("autofix isolated unit test timed out") from None
    finally:
        with contextlib.suppress(Exception):
            await _run_docker_cmd("rm", "-f", name, timeout=15, output_limit=4096)
    output = (stdout + stderr).decode("utf-8", errors="replace")
    if code != 0:
        raise RuntimeError(f"autofix isolated unit test failed:\n{output[-2000:]}")
    return output[-500:]


async def _open_pull_request(
    *,
    repo_dir: Path,
    branch: str,
    title: str,
    body: str,
) -> str:
    if is_protected_branch(branch):
        raise RuntimeError(f"refusing PR from protected branch {branch!r}")
    gh = _which("gh")
    if gh:
        proc = await asyncio.create_subprocess_exec(
            gh,
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            url = (stdout or b"").decode().strip().splitlines()[-1]
            if url.startswith("http"):
                return url
        logger.warning("gh pr create failed: %s", (stderr or stdout or b"").decode()[:400])
    token = (
        os.getenv("GITHUB_TOKEN")
        or os.getenv("GH_TOKEN")
        or os.getenv("MAXWELL_GITHUB_TOKEN")
        or ""
    ).strip()
    parsed = origin_github_repo(repo_dir)
    if not token or not parsed:
        raise RuntimeError(
            "cannot open PR: need `gh` or GITHUB_TOKEN plus a github.com origin"
        )
    owner, name = parsed
    import aiohttp

    payload = {
        "title": title,
        "head": branch,
        "base": "main",
        "body": body,
        "draft": False,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.github.com/repos/{owner}/{name}/pulls",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "maxwell-autofix",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300:
                raise RuntimeError(f"GitHub PR API {resp.status}: {str(data)[:400]}")
            return str(data.get("html_url") or "")


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _pr_body(
    *,
    patch: dict[str, Any],
    tool_name: str,
    tool_args: Any,
    tb_text: str,
    test_output: str | None,
) -> str:
    args_json = json.dumps(sanitize_tool_args(tool_args), ensure_ascii=False, indent=2)
    parts = [
        "## Autofix (Maxwell, not a sub-agent)",
        "",
        patch.get("pr_body") or patch.get("summary") or "",
        "",
        "### Failure",
        f"- Tool: `{tool_name}`",
        "- Exception: see traceback below",
        "",
        "### Sanitized tool arguments",
        "```json",
        args_json,
        "```",
        "",
        "### Traceback",
        "```",
        str(tb_text)[:MAX_TRACE_CHARS],
        "```",
        "",
        "This branch was created automatically. **Do not merge without a human review.** "
        "Maxwell never pushes to `main` and never auto-merges.",
    ]
    if test_output:
        parts.extend(["", "### pytest", "```", test_output, "```"])
    return redact_diagnostics("\n".join(parts).strip() + "\n")


async def run_autofix(
    bot: Any,
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    exc: BaseException,
    tb_text: str,
    fingerprint: str,
) -> str | None:
    token = _IN_AUTOFIX.set(True)
    worktree: Path | None = None
    branch = branch_name(type(exc).__name__)
    try:
        live_branch = current_branch(REPO_ROOT)
        now = time.time()
        state = _load_state(bot)
        fingerprints = dict(state.get("fingerprints") or {})
        fingerprints[fingerprint] = {
            "ts": now,
            "branch": branch,
            "pr_url": "",
            "tool": tool_name,
            "error": type(exc).__name__,
        }
        recent = [float(t) for t in (state.get("recent") or []) if t]
        recent = [t for t in recent if t >= now - 3600] + [now]
        _save_state(bot, {"fingerprints": fingerprints, "recent": recent[-50:]})
        context = gather_context(tb_text)
        messages = build_prompt(
            tool_name=tool_name,
            tool_args=tool_args,
            tb_text=tb_text,
            context=context,
        )
        raw = await _generate_patch(bot, messages)
        patch = parse_llm_patch(raw)
        worktree = _worktree_base(bot) / branch.replace("/", "-")
        if worktree.exists():
            raise RuntimeError(f"worktree already exists: {worktree}")
        _git(
            ["worktree", "add", "-b", branch, str(worktree), "HEAD"],
            cwd=REPO_ROOT,
            timeout=90,
        )
        if current_branch(worktree) != branch:
            raise RuntimeError(
                f"worktree landed on {current_branch(worktree)!r}, expected {branch!r}"
            )
        if is_protected_branch(current_branch(worktree)):
            raise RuntimeError("worktree is on a protected branch")
        if current_branch(REPO_ROOT) != live_branch:
            raise RuntimeError("autofix changed the live checkout branch")
        changed = apply_patch(patch, root=worktree)
        test_output = None
        try:
            test_output = await _run_pytest(
                worktree, [p for p in changed if p.startswith("tests/")]
            )
        except RuntimeError as test_exc:
            logger.warning("autofix tests failed for %s: %s", fingerprint, test_exc)
            raise
        _git(["add", "--", *changed], cwd=worktree)
        # Guard: nothing but the intended python files.
        staged = _git(["diff", "--cached", "--name-only"], cwd=worktree)
        staged_files = [ln.strip() for ln in staged.stdout.splitlines() if ln.strip()]
        for rel in staged_files:
            if allowed_relpath(rel, repo=worktree) is None:
                raise RuntimeError(f"refusing to commit blocked path {rel}")
        if current_branch(worktree) in PROTECTED_BRANCHES:
            raise RuntimeError("refusing to commit on a protected branch")
        message = patch["commit_message"] or f"fix: {tool_name} {type(exc).__name__}"
        _git(
            [
                "-c",
                "user.name=Maxwell Autofix",
                "-c",
                "user.email=maxwell-autofix@users.noreply.github.com",
                "commit",
                "-m",
                message,
            ],
            cwd=worktree,
        )
        # Push the topic branch only. `HEAD` here is the worktree's topic branch.
        if is_protected_branch(branch):
            raise RuntimeError("refusing to push protected branch")
        _git(["push", "-u", "origin", f"HEAD:{branch}"], cwd=worktree, timeout=120)
        pr_url = ""
        if _control_flag(bot, "autofix_open_pr", True):
            pr_url = await _open_pull_request(
                repo_dir=worktree,
                branch=branch,
                title=patch["pr_title"],
                body=_pr_body(
                    patch=patch,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tb_text=tb_text,
                    test_output=test_output,
                ),
            )
        now = time.time()
        state = _load_state(bot)
        fingerprints = dict(state.get("fingerprints") or {})
        fingerprints[fingerprint] = {
            "ts": now,
            "branch": branch,
            "pr_url": pr_url,
            "tool": tool_name,
            "error": type(exc).__name__,
        }
        recent = [float(t) for t in (state.get("recent") or []) if t]
        recent = [t for t in recent if t >= now - 3600] + [now]
        _save_state(bot, {"fingerprints": fingerprints, "recent": recent[-50:]})
        logger.warning(
            "autofix opened %s for tool=%s error=%s fingerprint=%s",
            pr_url or branch,
            tool_name,
            type(exc).__name__,
            fingerprint,
        )
        return pr_url or branch
    except Exception:
        logger.exception(
            "autofix failed tool=%s error=%s fingerprint=%s",
            tool_name,
            type(exc).__name__,
            fingerprint,
        )
        return None
    finally:
        _IN_AUTOFIX.reset(token)
        _active_fingerprints.discard(fingerprint)
        if worktree is not None:
            try:
                _git(["worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
            except Exception:
                logger.debug("autofix worktree cleanup failed", exc_info=True)


def schedule_tool_autofix(
    bot: Any,
    *,
    tool_name: str,
    tool_args: dict[str, Any] | None,
    exc: BaseException,
) -> bool:
    """Queue a background self-heal. Never blocks the live Discord turn."""
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        fp = should_schedule(bot, tool_name, exc, tb_text)
    except Exception:
        logger.debug("autofix should_schedule failed", exc_info=True)
        return False
    if not fp:
        return False
    _active_fingerprints.add(fp)

    async def _runner():
        async with _schedule_lock:
            await run_autofix(
                bot,
                tool_name=tool_name,
                tool_args=dict(tool_args or {}),
                exc=exc,
                tb_text=tb_text,
                fingerprint=fp,
            )

    try:
        _spawn_background(_runner())
        logger.info(
            "autofix scheduled tool=%s error=%s fingerprint=%s",
            tool_name,
            type(exc).__name__,
            fp,
        )
        return True
    except Exception:
        _active_fingerprints.discard(fp)
        logger.debug("autofix spawn failed", exc_info=True)
        return False
