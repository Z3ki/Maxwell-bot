"""Interactive per-user GitHub repository tools with isolated workspaces."""
from __future__ import annotations

import asyncio, base64, contextlib, hashlib, hmac, json, os, re, secrets, shlex, time, uuid
from urllib.parse import urlencode
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, ClassVar

import aiohttp

from tools import Tool
from utils import FileLock, _atomic_json_write_sync, controller_id, docker_bind_path
from plugins.github_projects.inspection import INSPECTION_SCRIPT
from plugins.github_projects.credential_git import GIT_SCRIPT, validate_git_commands

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,180}$")
_IMAGE = "maxwell-github-workspace:1"
_API = "https://api.github.com"
_PAT_RE = re.compile(r"^(ghp_|github_pat_|gho_|ghu_)[A-Za-z0-9_]{20,}$")
_ALLOWED_SCOPES = frozenset({
    "repo","public_repo","repo:status","repo_deployment","workflow",
    "write:packages","read:packages","gist","user","read:user","user:email",
    "read:org","notifications","project","read:project",
    "admin:repo_hook","write:repo_hook","read:repo_hook",
})
_DEFAULT_SCOPES = ("repo",)
_RETIRED_POLICY_KEYS = frozenset({
    "auto_review", "auto_merge", "auto_issue_reply", "schedule_enabled",
    "schedule_minutes", "schedule_goal",
})


def oauth_client_id() -> str:
    return str(os.getenv("MAXWELL_GITHUB_OAUTH_CLIENT_ID", "") or "").strip()


def oauth_client_secret() -> str:
    return str(os.getenv("MAXWELL_GITHUB_OAUTH_CLIENT_SECRET", "") or "").strip()


def oauth_redirect_uri() -> str:
    override = str(os.getenv("MAXWELL_GITHUB_OAUTH_REDIRECT_URI", "") or "").strip()
    if override:
        return override
    base = str(os.getenv("MAXWELL_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("MAXWELL_PUBLIC_BASE_URL is required for GitHub login links")
    return base + "/api/github/oauth/callback"


def oauth_configured() -> bool:
    return bool(oauth_client_id() and oauth_client_secret() and str(os.getenv("MAXWELL_PUBLIC_BASE_URL", "") or "").strip())


def normalize_scopes(raw: Any = None) -> list[str]:
    if raw is None or str(raw).strip() == "":
        parts = list(_DEFAULT_SCOPES)
    elif isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw]
    else:
        parts = re.split(r"[,\s]+", str(raw).strip())
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if part not in _ALLOWED_SCOPES:
            raise ValueError(
                f"unsupported GitHub permission {part!r}. Allowed: {', '.join(sorted(_ALLOWED_SCOPES))}"
            )
        if part not in out:
            out.append(part)
    return out or list(_DEFAULT_SCOPES)


def sign_oauth_state(uid: str, scopes: list[str] | None = None) -> str:
    uid = str(uid or "").strip()
    if not uid.isdigit():
        raise ValueError("oauth state requires a Discord user id")
    secret = oauth_client_secret().encode()
    if not secret:
        raise RuntimeError("GitHub OAuth is not configured")
    issued_at = time.time()
    payload = json.dumps({"u": uid, "s": normalize_scopes(scopes), "i": issued_at, "e": int(issued_at) + 600}, separators=(",", ":"))
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return body + "." + sig


def verify_oauth_state(state: str) -> dict[str, Any]:
    raw = str(state or "")
    if "." not in raw:
        raise ValueError("invalid oauth state")
    body, sig = raw.rsplit(".", 1)
    secret = oauth_client_secret().encode()
    expected = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest() if secret else ""
    try:
        valid = bool(secret) and hmac.compare_digest(sig, expected)
    except Exception:
        valid = False
    if not valid:
        raise ValueError("invalid oauth state")
    pad = "=" * (-len(body) % 4)
    data = json.loads(base64.urlsafe_b64decode(body + pad))
    if int(data.get("e") or 0) < time.time():
        raise ValueError("oauth login link expired; ask Maxwell for a new one")
    uid = str(data.get("u") or "")
    if not uid.isdigit():
        raise ValueError("invalid oauth state")
    return {"uid": uid, "scopes": normalize_scopes(data.get("s")), "issued_at": float(data["i"])}


def _oauth_pending_path() -> Path:
    from config import Config

    return Path(Config.DATA_DIR) / "plugins" / "github_projects" / "oauth_pending.json"


def _new_confirmation_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def stage_oauth_login(
    uid: str,
    token: str,
    *,
    login: str,
    scopes: list[str],
    issued_at: float,
) -> str:
    """Store an unconfirmed login. The browser callback must not publish it."""
    uid = str(uid or "").strip()
    token = str(token or "").strip()
    if not uid.isdigit() or not token:
        raise ValueError("invalid staged GitHub login")
    path = _oauth_pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    code = _new_confirmation_code()
    with FileLock(path, timeout=5):
        from privacy import owned_write_allowed

        if not owned_write_allowed(path.parent.parent.parent, uid, issued_at):
            raise ValueError("Request predates a privacy deletion; submit a new request")
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pending = {}
        if not isinstance(pending, dict):
            pending = {}
        pending[uid] = {
            "token": token,
            "login": str(login or "")[:80],
            "scopes": normalize_scopes(scopes),
            "issued_at": issued_at,
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "expires_at": time.time() + 600,
        }
        os.chmod(path.parent, 0o700)
        _json_atomic(path, pending)
        os.chmod(path, 0o600)
    return code


def confirm_oauth(uid: str, code: str) -> str:
    """Publish a staged login only for the Discord user who received its code."""
    uid = str(uid or "").strip()
    submitted = str(code or "").strip()
    if not uid.isdigit() or not re.fullmatch(r"\d{6}", submitted):
        raise ValueError("GitHub confirmation code must be six digits")
    path = _oauth_pending_path()
    with FileLock(path, timeout=5):
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pending = {}
        row = pending.get(uid) if isinstance(pending, dict) else None
        if not isinstance(row, dict) or float(row.get("expires_at") or 0) < time.time():
            raise ValueError("No pending GitHub login; authorize the link again")
        expected = str(row.get("code_hash") or "")
        if not hmac.compare_digest(hashlib.sha256(submitted.encode()).hexdigest(), expected):
            raise ValueError("GitHub confirmation code does not match")
        store = TokenStore(path.with_name("user_tokens.json"))
        store.write(
            uid,
            str(row.get("token") or ""),
            login=row.get("login"),
            scopes=row.get("scopes"),
            source="oauth",
            started_at=float(row.get("issued_at") or 0),
        )
        del pending[uid]
        _json_atomic(path, pending)
    return str(row.get("login") or "GitHub")


def authorize_url(uid: str, scopes: Any = None) -> str:
    if not oauth_configured():
        raise RuntimeError("GitHub OAuth is not configured")
    scopes = normalize_scopes(scopes)
    params = {
        "client_id": oauth_client_id(),
        "redirect_uri": oauth_redirect_uri(),
        "scope": " ".join(scopes),
        "state": sign_oauth_state(uid, scopes),
        "allow_signup": "true",
    }
    return "https://github.com/login/oauth/authorize?" + urlencode(params)


def _uid(message: Any) -> str:
    uid = str(getattr(getattr(message, "author", None), "id", "") or "").strip()
    if not uid:
        raise PermissionError("GitHub actions require an authenticated user context")
    return uid


def _repo(raw: Any) -> str:
    value = str(raw or "").strip().strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not _REPO_RE.fullmatch(value) or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("repo must be owner/name")
    return value


def _ref(raw: Any, *, default: str = "") -> str:
    value = str(raw or default or "").strip()
    if value and (
        not _SAFE_REF_RE.fullmatch(value)
        or ".." in value
        or "@{" in value
        or value.startswith(("/", ".", "-"))
        or value.endswith(("/", ".", ".lock"))
        or "//" in value
    ):
        raise ValueError("invalid git ref")
    return value


def _clip(value: Any) -> str:
    """Keep complete repository output (historical helper name)."""
    return str(value or "")


def _json_atomic(path: Path, data: Any) -> None:
    _atomic_json_write_sync(path, data)


class PolicyStore:
    def __init__(self, path: Path):
        self.path, self.lock = path, asyncio.Lock()
    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
    async def get(self, uid: str, repo: str) -> dict[str, Any]:
        async with self.lock:
            row = ((self._read().get(uid) or {}).get(repo) or {})
            return {k: v for k, v in row.items() if k not in _RETIRED_POLICY_KEYS} if isinstance(row, dict) else {}
    async def all(self) -> dict[str, Any]:
        async with self.lock:
            return self._read()
    def _set_sync(self, uid, repo, changes, started_at):
        with FileLock(self.path, timeout=5):
            from privacy import owned_write_allowed
            if not owned_write_allowed(self.path.parent.parent.parent, uid, started_at):
                raise ValueError("Request predates a privacy deletion; submit a new request")
            data = self._read()
            users = data.setdefault(uid, {})
            current = users.get(repo)
            current = dict(current) if isinstance(current, dict) else {}
            current.update(changes)
            current = {key: value for key, value in current.items() if key not in _RETIRED_POLICY_KEYS}
            current["updated_at"] = time.time()
            users[repo] = current
            _json_atomic(self.path, data)
            return dict(current)

    async def set(self, uid: str, repo: str, changes: dict[str, Any]) -> dict[str, Any]:
        from privacy import request_started_at
        async with self.lock:
            return await asyncio.to_thread(self._set_sync, uid, repo, changes, request_started_at())

class TokenStore:
    """Serialize token snapshots across the bot and OAuth API processes.

    Every mutation holds the same sidecar file lock from read through atomic
    replacement. Async callers run that transaction in a worker so lock
    contention and disk I/O cannot stall Discord's event loop.
    """

    def __init__(self, path: Path):
        self.path, self.lock = path, asyncio.Lock()
    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
    def peek(self, uid: str) -> str:
        row = self.row(uid)
        return str(row.get("token") or "").strip()
    def row(self, uid: str) -> dict[str, Any]:
        raw = self._read().get(str(uid))
        if isinstance(raw, dict):
            return dict(raw)
        if raw:
            return {"token": str(raw)}
        return {}
    def write(self, uid: str, token: str, *, started_at: float | None = None, **meta: Any) -> None:
        from privacy import owned_write_allowed, request_started_at
        if started_at is None:
            started_at = request_started_at()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path, timeout=5.0):
            if not owned_write_allowed(self.path.parent.parent.parent, uid, started_at):
                raise ValueError("Request predates a privacy deletion; submit a new request")
            data = self._read()
            row = {"token": token, "updated_at": time.time()}
            for key, value in meta.items():
                if value is not None:
                    row[key] = value
            data[str(uid)] = row
            _json_atomic(self.path, data)
    async def set(self, uid: str, token: str, *, started_at: float | None = None, **meta: Any) -> None:
        from privacy import request_started_at
        if started_at is None:
            started_at = request_started_at()
        async with self.lock:
            await asyncio.to_thread(self.write, uid, token, started_at=started_at, **meta)
    def _clear_sync(self, uid: str, *, started_at: float | None = None) -> bool:
        from privacy import owned_write_allowed, request_started_at
        if started_at is None:
            started_at = request_started_at()
        with FileLock(self.path, timeout=5.0):
            if not owned_write_allowed(self.path.parent.parent.parent, uid, started_at):
                raise ValueError("Request predates a privacy deletion; submit a new request")
            data = self._read()
            existed = str(uid) in data
            if existed:
                del data[str(uid)]
                _json_atomic(self.path, data)
            return existed
    async def clear(self, uid: str, *, started_at: float | None = None) -> bool:
        from privacy import request_started_at
        if started_at is None:
            started_at = request_started_at()
        async with self.lock:
            return await asyncio.to_thread(self._clear_sync, uid, started_at=started_at)


class RepoKnowledge:
    def __init__(self, path: Path, bot: Any | None = None):
        self.path, self.bot, self.lock = path, bot, asyncio.Lock()
    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
    def _update_sync(self, uid, repo, facts, started_at):
        with FileLock(self.path, timeout=5):
            from privacy import owned_write_allowed
            if not owned_write_allowed(self.path.parent.parent.parent, uid, started_at):
                raise ValueError("Request predates a privacy deletion; submit a new request")
            data = self._read()
            key = f"{uid}:{repo.lower()}"
            row = data.get(key)
            if not isinstance(row, dict):
                row = {"user_id": uid, "repo": repo, "events": []}
            row.update({key: value for key, value in facts.items() if value is not None})
            row["updated_at"] = time.time()
            events = row.get("events") if isinstance(row.get("events"), list) else []
            if facts.get("event"):
                events.append({"at": time.time(), "text": str(facts["event"])[:600]})
            row["events"] = events[-80:]
            data[key] = row
            _json_atomic(self.path, data)

    async def update(self, uid: str, repo: str, **facts: Any) -> None:
        from privacy import request_started_at
        async with self.lock:
            await asyncio.to_thread(self._update_sync, uid, repo, facts, request_started_at())
    async def get(self, uid: str, repo: str) -> dict[str, Any]:
        async with self.lock:
            row = self._read().get(f"{uid}:{repo.lower()}") or {}
            return dict(row) if isinstance(row, dict) else {}


@dataclass
class ExecResult:
    code: int; stdout: str; stderr: str
    def render(self) -> str:
        body = self.stdout.strip()
        if self.stderr.strip(): body += ("\n" if body else "") + "[stderr]\n" + self.stderr.strip()
        return f"exit={self.code}\n{_clip(body)}".rstrip()


class GitHubProjectService:
    def __init__(self, bot: Any, ctx: Any = None):
        self.bot, self.ctx = bot, ctx
        base = Path(ctx.data_dir) if ctx is not None else Path(getattr(getattr(bot,"config",None),"DATA_DIR","data"))/"plugins"/"github_projects"
        self.data_dir = base; self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root = self.data_dir/"workspaces"; self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.policy = PolicyStore(self.data_dir/"policies.json")
        self.user_tokens = TokenStore(self.data_dir/"user_tokens.json")
        self.knowledge = RepoKnowledge(self.data_dir/"repo_knowledge.json", bot)
        self._docker_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        return self._session

    async def close(self) -> None:
        """Release the shared HTTP session (called from the plugin teardown)."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    def token(self, uid: str, identity: str = "user") -> str:
        if identity == "bot": return str(os.getenv("MAXWELL_GITHUB_TOKEN","") or "").strip()
        safe = re.sub(r"[^0-9A-Za-z]", "_", uid)
        env = str(os.getenv(f"MAXWELL_GITHUB_USER_TOKEN_{safe}","") or "").strip()
        if env: return env
        return self.user_tokens.peek(uid)

    def _require_identity_access(self, uid: str, identity: str) -> None:
        if identity == "bot":
            # The shared operator token can see private/org repos that the
            # caller cannot. Check live admin status on every call, not only
            # when the policy was first saved.
            checker = getattr(self.bot, "_is_admin", None)
            allowed = False
            if callable(checker):
                with contextlib.suppress(Exception):
                    allowed = bool(checker(uid))
            if not allowed:
                raise PermissionError(
                    "the shared bot GitHub identity is available to Maxwell admins only"
                )

    async def _api(self, uid: str, policy: dict[str, Any], method: str, path: str, *, json_body: Any=None, accept: str="application/vnd.github+json") -> tuple[int,str,Any]:
        identity = str(policy.get("identity") or "user")
        self._require_identity_access(uid, identity)
        token = self.token(uid, identity)
        if not token:
            if identity=="bot":
                raise PermissionError("no GitHub credential configured; set MAXWELL_GITHUB_TOKEN")
            raise PermissionError("this Discord user has not logged into GitHub. Call github_repo action=auth to generate a login link.")
        headers = {"Authorization":f"Bearer {token}","Accept":accept,"X-GitHub-Api-Version":"2022-11-28","User-Agent":"Maxwell-GitHub-Projects/1.0"}
        async with (await self.session()).request(method.upper(), _API+path, headers=headers, json=json_body) as resp:
            text = await resp.text(); parsed = None
            if text:
                with contextlib.suppress(json.JSONDecodeError): parsed = json.loads(text)
            return resp.status, text, parsed

    @staticmethod
    def _container(uid: str) -> str:
        # Controller-scoped: Dev and Production share one Docker daemon but
        # must never adopt, restart, or remove each other's workspaces.
        return "maxwell-gh-" + hashlib.sha256(
            (f"{uid}\0{controller_id()}" if controller_id() else uid).encode()
        ).hexdigest()[:16]
    def user_root(self, uid: str) -> Path:
        root = self.workspace_root.resolve() / hashlib.sha256(uid.encode()).hexdigest()[:24]
        if root.resolve() != root:
            raise ValueError("workspace escaped user root")
        root.mkdir(parents=True, exist_ok=True)
        return root
    def repo_root(self, uid: str, repo: str) -> Path:
        owner, name = _repo(repo).split("/", 1)
        user_root = self.user_root(uid)
        root = user_root / "repos" / owner / name
        if user_root not in root.resolve().parents:
            raise ValueError("workspace escaped user root")
        # The guest can rename these directories concurrently. Never let a
        # host mkdir follow a guest-controlled parent symlink.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(user_root, flags)
        try:
            for part in ("repos", owner):
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        finally:
            os.close(descriptor)
        return root

    async def _proc(
        self, *args: str, timeout: int = 120, env: dict[str, str] | None = None,
        input: bytes | None = None, on_timeout: Callable[[], Any] | None = None,
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input=input), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                if on_timeout is not None:
                    with contextlib.suppress(Exception):
                        await on_timeout()
            finally:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                # A cancelled communicate can leave pipe transports paused at
                # their buffer limit. Drain them before waiting for process exit.
                with contextlib.suppress(Exception):
                    await proc.communicate()
                with contextlib.suppress(Exception):
                    await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(f"command timed out after {timeout}s") from None
        return ExecResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))

    async def _kill_workspace_exec(self, container_id: str) -> None:
        """Stop the guest from the host. In-container cleanup can be blocked."""
        with contextlib.suppress(Exception):
            await self._proc("docker", "kill", container_id, timeout=20)
        with contextlib.suppress(Exception):
            await self._proc("docker", "rm", "-f", container_id, timeout=20)

    async def _ensure_image(self)->None:
        check=await self._proc("docker","image","inspect",_IMAGE,timeout=20)
        if check.code==0:return
        built=await self._proc("docker","build","-t",_IMAGE,str(Path(__file__).resolve().parent),timeout=900)
        if built.code!=0: raise RuntimeError("could not build GitHub workspace image: "+built.render())

    async def ensure_container(self,uid:str)->str:
        async with self._docker_lock:
            await self._ensure_image(); name=self._container(uid)
            inspect=await self._proc("docker","inspect","-f","{{.State.Running}}",name,timeout=20)
            if inspect.code==0:
                if inspect.stdout.strip().lower()=="true": return name
                if (await self._proc("docker","start",name,timeout=30)).code==0:return name
                await self._proc("docker","rm","-f",name,timeout=30)
            root=docker_bind_path(self.user_root(uid))
            args=["docker","run","-d","--init","--name",name,"--network","bridge","--memory","4g","--memory-swap","4g","--cpus","2","--pids-limit","768","--security-opt","no-new-privileges","--cap-drop","ALL","--cap-add","CHOWN","--cap-add","DAC_OVERRIDE","--cap-add","FOWNER","--cap-add","SETGID","--cap-add","SETUID","--tmpfs","/tmp:rw,exec,nosuid,size=256m","-v",f"{root}:/workspace:rw",_IMAGE]
            res=await self._proc(*args,timeout=60)
            if res.code!=0: raise RuntimeError("could not start user workspace: "+res.render())
            return name

    async def _require_command_permission(self, uid: str, repo: str) -> None:
        """Custom programs cannot be classified safely from shell source text."""
        policy = await self.policy.get(uid, _repo(repo))
        if policy.get("allow_security_testing") is not True:
            raise PermissionError(
                "repository code execution is disabled by repo policy; "
                "set allow_security_testing=true for this repository to enable "
                "custom commands, security testing, and Git checkout/commit/push. "
                "Service Git ignores repository hooks and executable configuration"
            )

    async def run(self,uid:str,repo:str,command:str,*,timeout:int=900,env:dict[str,str]|None=None)->ExecResult:
        repo = _repo(repo)
        command = str(command or "").strip()
        if not command:
            raise ValueError("command is required")
        await self._require_command_permission(uid, repo)
        return await self._run_workspace_command(
            uid, repo, command, timeout=timeout, env=env
        )

    async def _run_workspace_command(self,uid:str,repo:str,command:str,*,timeout:int=900,env:dict[str,str]|None=None)->ExecResult:
        """Transport for a custom request already authorized by ``run``.

        Built-in inspection uses a separate, read-only container. Model-supplied
        command text must never enter this transport without the ``run`` gate.
        """
        repo=_repo(repo); root=self.repo_root(uid,repo)
        if not root.exists(): raise FileNotFoundError("repo is not checked out yet")
        container=await self.ensure_container(uid); owner,name=repo.split("/",1); argv=["docker","exec","--workdir",f"/workspace/repos/{owner}/{name}"]
        for k,v in (env or {}).items(): argv += ["-e",f"{k}={v}"]
        ident = await self._proc("docker", "inspect", "-f", "{{.Id}}", container, timeout=20)
        container_id = ident.stdout.strip() if ident.code == 0 else ""
        if not container_id:
            raise RuntimeError("could not identify user workspace")
        argv += [container_id, "bash", "-lc", command]

        return await self._proc(
            *argv,
            timeout=max(1, min(int(timeout), 3600)),
            on_timeout=lambda: self._kill_workspace_exec(container_id),
        )

    async def inspect_repository(
        self, uid: str, repo: str, operation: str, *, ref: str = "", timeout: int = 120
    ) -> ExecResult:
        """Read a checkout without executing its Git configuration or helpers."""
        if operation not in {"status", "diff", "verify", "checkout_head"}:
            raise ValueError("unsupported repository inspection")
        repo = _repo(repo)
        ref = _ref(ref)
        root = self.repo_root(uid, repo)
        if not root.exists():
            raise FileNotFoundError("repo is not checked out yet")
        container = self._container(uid) + "-inspect-" + uuid.uuid4().hex
        await self._ensure_image()
        args = [
            "docker", "run", "--rm", "--name", container, "--init", "--read-only", "--network", "none",
            "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
            "--pids-limit", "64", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
            "-v", f"{docker_bind_path(self.user_root(uid))}:/workspace:ro", "--workdir", "/tmp", _IMAGE,
            "python3", "-I", "-c", INSPECTION_SCRIPT, operation, f"/workspace/repos/{repo}", ref,
        ]
        return await self._proc(
            *args, timeout=max(1, min(int(timeout), 180)),
            on_timeout=lambda: self._remove_sidecar(container),
        )

    async def _remove_sidecar(self, container: str) -> None:
        """Remove only the unique container created for this invocation."""
        await self._proc("docker", "rm", "-f", container, timeout=15)

    async def git(self,uid:str,repo:str,policy:dict[str,Any],commands:list[list[str]],*,timeout:int=300)->ExecResult:
        repo=_repo(repo)
        await self._require_command_permission(uid, repo)
        validate_git_commands(commands)
        target=self.repo_root(uid,repo)
        if not target.exists(): raise FileNotFoundError("repo is not checked out yet")
        return await self._trusted_git(uid, repo, policy, commands, timeout=timeout)

    async def _trusted_git(
        self, uid: str, repo: str, policy: dict[str, Any],
        commands: list[list[str]], *, checkout: bool = False, timeout: int = 300,
    ) -> ExecResult:
        """Execute only trusted Git with a private snapshot and stdin credential."""
        validate_git_commands(commands)
        identity = str(policy.get("identity") or "user")
        self._require_identity_access(uid, identity)
        await self._ensure_image()
        self._require_identity_access(uid, identity)
        token = self.token(uid, identity)
        if not token:
            raise PermissionError("GitHub credential is not configured")
        self.repo_root(uid, repo)
        cache_volume = "maxwell-git-cache-" + hashlib.sha256(
            f"{uid}\0{repo}".encode()
        ).hexdigest()[:24]
        container = self._container(uid) + "-git-" + uuid.uuid4().hex
        args = [
            "docker", "run", "--rm", "--name", container, "--init", "--interactive", "--read-only",
            "--network", "bridge", "--memory", "2g", "--memory-swap", "2g",
            "--cpus", "1.5", "--pids-limit", "384",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--mount", f"type=volume,source={cache_volume},destination=/git-scratch",
            "-v", f"{docker_bind_path(self.user_root(uid))}:/workspace:rw", "--workdir", "/tmp", _IMAGE,
            "python3", "-I", "-c", GIT_SCRIPT, f"/workspace/repos/{repo}", "/git-scratch",
        ]
        payload = json.dumps({
            "repo": repo, "commands": commands, "checkout": checkout, "token": token,
        }).encode()
        result = await self._proc(
            *args, input=payload, timeout=max(1, min(int(timeout), 1800)),
            on_timeout=lambda: self._remove_sidecar(container),
        )
        return ExecResult(result.code, result.stdout.replace(token, "[redacted]"), result.stderr.replace(token, "[redacted]"))

    async def checkout(self,uid:str,repo:str,policy:dict[str,Any],ref:str="")->str:
        repo=_repo(repo)
        await self._require_command_permission(uid, repo)
        ref = _ref(ref)
        target=self.repo_root(uid,repo)
        commands = [["fetch", "--prune", "origin"]]
        if ref:
            commands.append(["checkout", ref])
        commands.append(["status", "--short", "--branch"])
        res = await self._trusted_git(uid, repo, policy, commands, checkout=True, timeout=600)
        if res.code!=0: raise RuntimeError(res.render())
        head=await self.inspect_repository(uid,repo,"checkout_head",timeout=60)
        await self.knowledge.update(uid,repo,event="checkout/sync",workspace=str(target),head=head.stdout[:1000]); return _clip(head.render())

    async def checkout_pr(self,uid:str,repo:str,policy:dict[str,Any],number:int)->str:
        if number<=0: raise ValueError("PR number must be positive")
        branch=f"maxwell/pr-{number}"
        commands = [
            ["fetch", "origin", f"+pull/{number}/head:refs/remotes/origin/pr/{number}"],
            ["checkout", "-B", branch, f"refs/remotes/origin/pr/{number}"],
            ["status", "--short", "--branch"],
        ]
        out=await self.git(uid,repo,policy,commands,timeout=300)
        if out.code==0: await self.knowledge.update(uid,repo,event=f"checked out PR #{number}")
        return out.render()

    @staticmethod
    def mode(policy:dict[str,Any])->str:
        v=str(policy.get("mode") or "read").lower(); return v if v in {"read","write","admin"} else "read"
    @classmethod
    def require_mode(cls,policy:dict[str,Any],minimum:str)->None:
        order={"read":0,"write":1,"admin":2}
        if order[cls.mode(policy)]<order[minimum]: raise PermissionError(f"repo policy requires mode={minimum} (current {cls.mode(policy)})")

    async def verify(self,uid:str,repo:str,command:str="")->str:
        repo = _repo(repo)
        if command.strip():
            # Refuse before even the built-in inspection starts, then ``run``
            # rechecks the current policy immediately before the custom command.
            await self._require_command_permission(uid, repo)
        inspection = await self.inspect_repository(uid, repo, "verify", timeout=120)
        parts = [inspection.render()]
        if command.strip(): parts.append("[tests]\n"+(await self.run(uid,repo,command,timeout=1800)).render())
        return "\n\n".join(parts)


class GitHubRepoTool(Tool):
    tool_name="github_repo"; returns_result=True; ends_turn=False; is_destructive=True
    required_capabilities=("network","files.read","files.write","shell","secrets.read"); timeout_seconds=3600
    parameters: ClassVar[dict[str, Any]] = {"type":"object","properties":{
        "action":{"type":"string","enum":["auth","auth_set","auth_clear","policy_get","policy_set","list","checkout","sync","status","run","diff","verify","commit","push","pr_create","pr_get","pr_diff","pr_checkout","review","merge","issue_get","issue_reply","knowledge"]},
        "repo":{"type":"string"},"ref":{"type":"string"},"command":{"type":"string"},"commit_message":{"type":"string","description":"Required commit text for action=commit."},"title":{"type":"string"},"body":{"type":"string"},"token":{"type":"string"},"scopes":{"type":"string"},"permissions":{"type":"string"},"number":{"type":"integer"},"event":{"type":"string","enum":["COMMENT","APPROVE","REQUEST_CHANGES"]},"mode":{"type":"string","enum":["read","write","admin"]},"identity":{"type":"string","enum":["user","bot"]},"allow_security_testing":{"type":"boolean","description":"Explicitly enable custom run/verify commands and Git checkout/sync/PR checkout/commit/push for this user and repository. Disabled by default; read-only inspection remains available. Service Git ignores repository hooks and configuration."},"merge_method":{"type":"string","enum":["merge","squash","rebase"]},"base":{"type":"string"},"head":{"type":"string"}},"required":["action"],"additionalProperties":True}
    def __init__(self,bot,service): super().__init__(bot); self.service=service
    def get_description(self):
        return (
            "Isolated per-user GitHub workspace. auth generates a login link with selectable scopes "
            "(default repo); actions include auth_clear, policy_get/set, list, checkout/sync/status/run/"
            "diff/verify/commit/push, pr_create/get/diff/pr_checkout/review/merge, issue_get/reply, "
            "knowledge. Repo policy governs write/admin. Custom run/verify and Git checkout/sync/"
            "pr_checkout/commit/push require explicit allow_security_testing=true for this user and "
            "repository; built-in status/diff/verify inspection does not. Service Git ignores repo "
            "hooks/config; normal shell commands never receive GitHub tokens. commit requires "
            "commit_message; review/issue_reply use body."
        )

    async def execute(self,message:Any,action:str|None=None,repo:str|None=None,**kw:Any)->str:
        try:
            return await self._execute(message, action, repo, **kw)
        except (PermissionError, FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
            return f"Error: {exc}"

    async def _execute(self,message:Any,action:str|None=None,repo:str|None=None,**kw:Any)->str:
        uid=_uid(message); action=str(action or "").strip().lower()
        if action == "schedule_set" or _RETIRED_POLICY_KEYS.intersection(kw):
            return "Error: scheduled repository agents and automatic maintenance were removed"
        if action in {"auth","auth_status","connect"}:
            scopes = normalize_scopes(kw.get("scopes") or kw.get("permissions") or kw.get("scope"))
            row = self.service.user_tokens.row(uid)
            login = str(row.get("login") or "")
            connected = "yes" + (f" as {login}" if login else "") if (self.service.token(uid) or login) else "no"
            confirmation = str(kw.get("confirmation_code") or kw.get("code") or "").strip()
            if confirmation:
                try:
                    confirmed = await asyncio.to_thread(confirm_oauth, uid, confirmation)
                except (OSError, ValueError) as exc:
                    return f"Error: {exc}"
                return f"GitHub login confirmed for Discord user {uid} as {confirmed}."
            try:
                url = authorize_url(uid, scopes)
            except Exception as exc:
                return f"Error: cannot generate GitHub login link: {exc}"
            return (
                f"GitHub login for Discord user {uid}. connected={connected}. scopes={','.join(scopes)}.\n"
                f"Click this link and authorize Maxwell. A private confirmation code will be DM'd to this Discord user; "
                f"then call auth again with confirmation_code. The browser callback alone does not connect the account.\n{url}"
            )
        if action=="auth_set":
            raw=str(kw.get("token") or kw.get("body") or "").strip()
            if not _PAT_RE.fullmatch(raw):
                return "Error: token must be a GitHub PAT starting with ghp_ or github_pat_. Create one at https://github.com/settings/tokens"
            await self.service.user_tokens.set(uid, raw)
            return f"GitHub PAT saved for Discord user {uid}. It will not be shown again."
        if action=="auth_clear":
            await self.service.user_tokens.clear(uid)
            return f"GitHub PAT removed for Discord user {uid}."
        if action=="policy_set":
            r=_repo(repo); changes={k:kw[k] for k in ("mode","identity","allow_security_testing","merge_method") if k in kw and kw[k] is not None}
            if changes.get("mode") not in (None,"read","write","admin"):return "Error: mode must be read, write, or admin"
            if changes.get("identity") not in (None,"user","bot"):return "Error: identity must be user or bot"
            if changes.get("merge_method") not in (None,"merge","squash","rebase"):return "Error: invalid merge_method"
            if "allow_security_testing" in changes and not isinstance(changes["allow_security_testing"], bool):
                return "Error: allow_security_testing must be a boolean"
            if changes.get("identity")=="bot":
                checker=getattr(self.bot,"_is_admin",None); ok=False
                if callable(checker):
                    with contextlib.suppress(Exception):ok=bool(checker(uid))
                if not ok:return "Error: shared bot GitHub identity can only be enabled by a Maxwell admin"
            changes["channel_id"]=str(getattr(getattr(message,"channel",None),"id","") or "")
            cur=await self.service.policy.get(uid,r); candidate=dict(cur); candidate.update(changes)
            try:
                status,text,data=await self.service._api(uid,candidate,"GET",f"/repos/{r}")
                if status!=200 or not isinstance(data, dict):
                    return f"Error: GitHub access check failed HTTP {status}: {_clip(text)}"
                perms=data.get("permissions") if isinstance(data.get("permissions"),dict) else {}; wanted=self.service.mode(candidate)
                if wanted in {"write","admin"} and not perms.get("push"):return "Error: selected credential lacks push permission"
                if wanted=="admin" and not perms.get("admin"):return "Error: selected credential lacks admin permission"
            except Exception as exc:return f"Error: GitHub access is not ready: {exc}"
            row=await self.service.policy.set(uid,r,changes); return "Policy saved and access verified:\n"+json.dumps(row,indent=2,ensure_ascii=False)
        if action=="list":
            pol={"identity":str(kw.get("identity") or "user")}; status,text,data=await self.service._api(uid,pol,"GET","/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member")
            if status!=200 or not isinstance(data,list):return f"Error: GitHub HTTP {status}: {_clip(text)}"
            return "Accessible repos:\n"+"\n".join(f"{x.get('full_name')} private={bool(x.get('private'))} default={x.get('default_branch')} pushed={x.get('pushed_at')}" for x in data[:100])
        r=_repo(repo); pol=await self.service.policy.get(uid,r)
        if not pol:return "Error: repository has no policy yet. Run policy_set first."
        if action=="policy_get":return json.dumps(pol,indent=2,ensure_ascii=False)
        try:
            if action in {"checkout","sync"}:return await self.service.checkout(uid,r,pol,_ref(kw.get("ref")))
            if action=="status":return (await self.service.inspect_repository(uid,r,"status",timeout=60)).render()
            if action=="run":
                cmd=str(kw.get("command") or "").strip()
                if not cmd:return "Error: command is required"
                return (await self.service.run(uid,r,cmd,timeout=int(kw.get("timeout") or 900))).render()
            if action=="diff":
                ref=_ref(kw.get("ref")); return _clip((await self.service.inspect_repository(uid,r,"diff",ref=ref,timeout=120)).render())
            if action=="verify":return await self.service.verify(uid,r,str(kw.get("command") or ""))
            if action=="commit":
                self.service.require_mode(pol,"write"); msg=str(kw.get("commit_message") or "").strip()
                if not msg:return "Error: commit_message is required"
                out=await self.service.git(uid,r,pol,[["add","-A"],["diff","--cached","--check"],["commit","-m",msg]],timeout=180)
                if out.code==0:await self.service.knowledge.update(uid,r,event=f"commit: {msg[:140]}")
                return out.render()
            if action=="push":
                self.service.require_mode(pol,"write"); ref=_ref(kw.get("ref")); out=await self.service.git(uid,r,pol,[["push","origin",ref or "HEAD"]],timeout=600)
                if out.code==0:await self.service.knowledge.update(uid,r,event=f"push {ref or 'HEAD'}")
                return out.render()
            if action=="pr_create":
                self.service.require_mode(pol,"write"); title=str(kw.get("title") or "").strip(); body=str(kw.get("body") or "").strip(); head=_ref(kw.get("head")); base=_ref(kw.get("base"))
                if not(title and head and base):return "Error: title, head, and base are required"
                status,text,data=await self.service._api(uid,pol,"POST",f"/repos/{r}/pulls",json_body={"title":title,"body":body,"head":head,"base":base})
                if status not in {200,201}:return f"Error: GitHub HTTP {status}: {_clip(text)}"
                await self.service.knowledge.update(uid,r,event=f"created PR #{data.get('number')}"); return json.dumps({k:data.get(k) for k in ("number","html_url","state","title")},indent=2)
            if action=="pr_get":
                n=int(kw.get("number") or 0); status,text,data=await self.service._api(uid,pol,"GET",f"/repos/{r}/pulls/{n}")
                if status!=200:return f"Error: GitHub HTTP {status}: {_clip(text)}"
                keep={k:data.get(k) for k in ("number","title","body","state","draft","mergeable","mergeable_state","html_url","updated_at")}; keep.update(head=(data.get("head") or {}).get("ref"),head_sha=(data.get("head") or {}).get("sha"),base=(data.get("base") or {}).get("ref")); return _clip(json.dumps(keep,indent=2,ensure_ascii=False))
            if action=="pr_diff":
                n=int(kw.get("number") or 0); status,text,_=await self.service._api(uid,pol,"GET",f"/repos/{r}/pulls/{n}",accept="application/vnd.github.v3.diff"); return _clip(text) if status==200 else f"Error: GitHub HTTP {status}: {_clip(text)}"
            if action=="pr_checkout":return await self.service.checkout_pr(uid,r,pol,int(kw.get("number") or 0))
            if action=="review":
                self.service.require_mode(pol,"write"); n=int(kw.get("number") or 0); event=str(kw.get("event") or "COMMENT").upper(); body=str(kw.get("body") or "").strip()
                if event not in {"COMMENT","APPROVE","REQUEST_CHANGES"}:return "Error: invalid review event"
                status,text,data=await self.service._api(uid,pol,"POST",f"/repos/{r}/pulls/{n}/reviews",json_body={"event":event,"body":body}); return f"Review submitted: {event} {data.get('html_url','')}" if status in {200,201} else f"Error: GitHub HTTP {status}: {_clip(text)}"
            if action=="merge":
                self.service.require_mode(pol,"admin"); n=int(kw.get("number") or 0); method=str(kw.get("merge_method") or pol.get("merge_method") or "squash")
                status,text,data=await self.service._api(uid,pol,"PUT",f"/repos/{r}/pulls/{n}/merge",json_body={"merge_method":method})
                if status!=200:return f"Error: GitHub HTTP {status}: {_clip(text)}"
                await self.service.knowledge.update(uid,r,event=f"merged PR #{n} via {method}"); return json.dumps(data,indent=2,ensure_ascii=False)
            if action=="issue_get":
                n=int(kw.get("number") or 0); status,text,data=await self.service._api(uid,pol,"GET",f"/repos/{r}/issues/{n}")
                if status!=200 or not isinstance(data,dict):return f"Error: GitHub HTTP {status}: {_clip(text)}"
                keep={k:data.get(k) for k in ("number","title","body","state","html_url","updated_at","comments")}; keep["user"]=(data.get("user") or {}).get("login"); keep["labels"]=[x.get("name") for x in (data.get("labels") or []) if isinstance(x,dict)]; return _clip(json.dumps(keep,indent=2,ensure_ascii=False))
            if action=="issue_reply":
                self.service.require_mode(pol,"write"); n=int(kw.get("number") or 0); body=str(kw.get("body") or "").strip()
                if not body:return "Error: body is required"
                status,text,data=await self.service._api(uid,pol,"POST",f"/repos/{r}/issues/{n}/comments",json_body={"body":body}); return f"Issue reply posted: {data.get('html_url','')}" if status in {200,201} else f"Error: GitHub HTTP {status}: {_clip(text)}"
            if action=="knowledge":return json.dumps(await self.service.knowledge.get(uid,r),indent=2,ensure_ascii=False)
            return "Error: unknown action"
        except (PermissionError,FileNotFoundError,ValueError,RuntimeError,OSError) as exc:return f"Error: {exc}"
