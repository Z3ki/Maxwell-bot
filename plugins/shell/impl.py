"""Tool implementations for the shell plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

import tempfile
from typing import ClassVar

from tooling import helpers as _helpers
from tools import Tool
from plugins.shell.isolation import (
    SANDBOX_LIMITS,
    ShellTenant,
    docker_run_args,
    egress_policy_ready,
    resource_pool_ready,
    shell_tenant,
    validate_runsc_runtime,
)
from utils import controller_id

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name


def _bounded_env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        raw = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        raw = default
    return max(low, min(raw, high))


class ShellTool(Tool):
    """Execute commands in a tenant-scoped gVisor sandbox."""
    tool_name = 'shell'
    returns_result = True
    ends_turn = False
    # The sandbox is a tenant capability. Guild administrators gain no host or
    # application-owner authority from using it.
    requires_admin = False


    # Shell executes arbitrary code in a container. It's the most dangerous
    # tool we expose, so it gets the taint-check / user-confirmation gate.
    is_destructive = True

    IMAGE_NAME = "maxwell-shell"
    DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker")
    # Bump when docker-run flags or recycle policy change so an old sandbox
    # is replaced instead of reused.
    _SANDBOX_INIT = "7"

    # Operator configuration may lower this limit, but cannot remove it.
    _TIMEOUT_CEILING_SECONDS = 900

    # Idle expiry destroys the tenant container and its transient workspace.
    _IDLE_SECONDS_DEFAULT = 600
    _MAX_CACHED_TENANTS = _bounded_env_int(
        "MAXWELL_SHELL_MAX_CACHED_TENANTS", 4, 1, 4
    )
    _MAX_CACHED_PER_OWNER = _bounded_env_int(
        "MAXWELL_SHELL_MAX_CACHED_PER_OWNER", 2, 1, 2
    )
    _last_used_by_tenant: ClassVar[dict[str, float]] = {}
    _tenant_locks: ClassVar[dict[str, asyncio.Lock]] = {}
    _user_slots: ClassVar[dict[str, asyncio.Semaphore]] = {}
    _active_tenants: ClassVar[set[str]] = set()
    _active_owners: ClassVar[set[str]] = set()
    _prepared_tenants: ClassVar[set[str]] = set()
    _tenants: ClassVar[dict[str, ShellTenant]] = {}
    _idle_reaper_task: ClassVar[asyncio.Task | None] = None
    _global_slots: ClassVar[asyncio.Semaphore] = asyncio.Semaphore(
        _bounded_env_int("MAXWELL_SHELL_MAX_CONCURRENCY", 4, 1, 8)
    )
    _state_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _recovery_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _recovery_complete: ClassVar[bool] = False
    # Persistent /storage bookkeeping, keyed by container name. "mb" is the
    # tmpfs size the guest was started with (0 = no /storage), "gen" is the
    # host generation last restored into it, "base" the manifest that
    # restore produced (merge base), "fingerprint" the guest's tree hash.
    _storage_state: ClassVar[dict[str, dict]] = {}
    _storage_notes: ClassVar[dict[str, str]] = {}
    # Detached jobs pin their tenant so the idle reaper can't wipe the
    # /workspace a subagent is still building in. Count, not bool: two jobs.
    _pins: ClassVar[dict[str, int]] = {}
    _PREMIUM_TIMEOUT_DEFAULT = 600
    # The reaper is a classmethod with no bot; tools register their bound
    # save so the last /storage writes survive an idle recycle.
    _idle_savers: ClassVar[dict[str, object]] = {}

    @classmethod
    def _max_output(cls) -> int:
        """Compatibility accessor: shell output is no longer clipped."""
        return 0

    @classmethod
    def _max_command_length(cls) -> int:
        """Compatibility accessor: command text has no application size cap."""
        return 0

    @classmethod
    def _timeout_seconds(cls) -> int:
        """Max wall-clock seconds for a shell command. Always > 0; capped at 1h."""
        raw = os.environ.get("MAXWELL_SHELL_TIMEOUT", "").strip()
        if not raw:
            return 600  # 10 min default — was 30s, way too tight for real work
        try:
            v = int(raw)
        except ValueError:
            return 600
        return max(1, min(v, cls._TIMEOUT_CEILING_SECONDS))

    def _is_premium(self, owner_id: str) -> bool:
        ledger = getattr(self.bot, "_message_quota", None) if self.bot is not None else None
        if ledger is None:
            return False
        try:
            return ledger.plan_for(str(owner_id)) == "premium"
        except Exception:
            return False

    @classmethod
    def _premium_timeout_seconds(cls) -> int:
        """Premium commands get at least this long; the ceiling still wins."""
        try:
            raw = int(os.environ.get("MAXWELL_SHELL_PREMIUM_TIMEOUT", "") or cls._PREMIUM_TIMEOUT_DEFAULT)
        except ValueError:
            raw = cls._PREMIUM_TIMEOUT_DEFAULT
        return max(1, min(raw, cls._TIMEOUT_CEILING_SECONDS))

    async def _command_timeout(self, tenant: ShellTenant) -> int:
        base = self._timeout_seconds()
        if self.bot is None:
            return base
        if await asyncio.to_thread(self._is_premium, tenant.owner_id):
            return max(base, self._premium_timeout_seconds())
        return base

    @classmethod
    def _idle_seconds(cls) -> int:
        """Seconds unused before the tenant sandbox is destroyed."""
        raw = os.environ.get("MAXWELL_SHELL_IDLE_SECONDS", "").strip()
        if not raw:
            return cls._IDLE_SECONDS_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._IDLE_SECONDS_DEFAULT
        return max(60, min(v, cls._TIMEOUT_CEILING_SECONDS))

    def limits_for(self, user_id: str) -> dict:
        """Read-only sandbox limits for get_configuration. Sync; called off-loop."""
        premium = self.bot is not None and self._is_premium(str(user_id))
        timeout = self._timeout_seconds()
        if premium:
            timeout = max(timeout, self._premium_timeout_seconds())
        storage = self._user_storage()
        quota = 0
        if storage is not None:
            try:
                quota = storage.quota(str(user_id))
            except Exception:
                quota = 0
        return {
            "shell": {
                "command_timeout_seconds": timeout,
                "idle_reset_seconds": self._idle_seconds(),
                "memory": SANDBOX_LIMITS["memory"],
                "cpus": SANDBOX_LIMITS["cpus"],
                "pids": SANDBOX_LIMITS["pids"],
                "workspace_tmpfs": SANDBOX_LIMITS["workspace"],
                "tmp_tmpfs": SANDBOX_LIMITS["tmp"],
                "persistent_storage_mb": quota // (1024 * 1024),
                "survives_idle_reset": ["/storage"] if quota else [],
                "wiped_on_idle_reset": ["/workspace", "/tmp", "installed packages", "exported env vars"],
                "can_reach_own_public_host": False,
            }
        }

    @staticmethod
    def _full_host_access() -> bool:
        """Retained for compatibility; public full-host mode is removed."""
        return False

    def get_description(self):
        # Surface live limits so the model doesn't have to guess. Pulled at
        # description-build time, which happens per-turn on tool registration.
        to = self._timeout_seconds()
        premium_to = max(to, self._premium_timeout_seconds())
        idle = self._idle_seconds()
        if idle:
            persist_note = (
                f"Sandbox, /workspace (512 MB) and /tmp (256 MB) are wiped after {idle}s idle, "
                "on timeout or cancel, and on bot restart; exported env vars never carry over "
                "between calls."
            )
        else:
            persist_note = "Container persists across calls."
        storage_note = (
            "Users with persistent storage (Premium) also get /storage: it survives every wipe, "
            "is saved after each command, and its quota is the tmpfs size. Save deliverables "
            "and anything worth keeping there, never caches or node_modules. Files in "
            "/storage/public/ are published at the user's /u/<handle>/ address. "
        )
        limits_note = (
            "Command text and captured stdout/stderr have no application size cap. "
            f"Channel progress shows status only. Timeout {to}s ({premium_to}s for Premium). "
            "2 GB RAM, 1 CPU, 256 PIDs. Outbound internet works, but the sandbox cannot connect "
            "to this bot's own public host; use fetch_url or site_server to test published sites. "
            f"{persist_note} {storage_note}"
        )
        how = (
            "To write a file, put the redirect on the opener line: "
            "`cat << 'EOF' > path/file.py` then the body then a line containing "
            "only EOF. `cmd` is an alias for `command`. Do not prefix `$ ` or "
            "wrap the command in a markdown fence. Attach outputs with files= "
            "(comma-separated paths under /workspace or /storage)."
        )
        return (
            "Run bash -lc as root inside this user's gVisor sandbox (workdir "
            "/workspace). Root is confined by runsc; it has no host capabilities "
            "and receives only a small in-guest capability set. Params: command (required), files "
            "(optional paths under /workspace or /storage to attach privately or publicly "
            "according to this request's visibility). Max 10 MB per file. "
            f"{how} {limits_note}"
        )

    @classmethod
    async def _run_docker(cls, *args: str, timeout: int = 30):
        return await _run_docker_cmd(*args, timeout=timeout)

    @classmethod
    def _lock_for(cls, tenant: ShellTenant) -> asyncio.Lock:
        lock = cls._tenant_locks.get(tenant.container_name)
        if lock is None:
            lock = asyncio.Lock()
            cls._tenant_locks[tenant.container_name] = lock
        return lock

    @classmethod
    def _user_slot(cls, tenant: ShellTenant) -> asyncio.Semaphore:
        slot = cls._user_slots.get(tenant.owner_id)
        if slot is None:
            slot = asyncio.Semaphore(1)
            cls._user_slots[tenant.owner_id] = slot
        return slot

    @classmethod
    def _runtime(cls) -> str:
        # Unsupported isolation is a hard error, never a Docker fallback.
        return os.environ.get("MAXWELL_SHELL_RUNTIME", "runsc").strip()

    @classmethod
    def _network(cls) -> str:
        return os.environ.get(
            "MAXWELL_SHELL_NETWORK", "maxwell-shell-egress"
        ).strip()

    @classmethod
    async def _wait_container_gone(cls, container_name: str) -> None:
        for _ in range(100):
            (_stdout, _stderr), inspect_code = await cls._run_docker(
                "inspect",
                "--type",
                "container",
                "-f",
                "{{.Id}}",
                container_name,
                timeout=10,
            )
            if inspect_code != 0:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(
            "sandbox container did not disappear after removal"
        )

    @classmethod
    async def _destroy_container_unlocked(cls, tenant: ShellTenant) -> None:
        """Destroy one tenant's sandbox; never target model supplied identifiers."""
        (_stdout, _stderr), rm_code = await cls._run_docker(
            "rm", "-f", tenant.container_name, timeout=10
        )
        if rm_code != 0:
            (_stdout, _stderr), inspect_code = await cls._run_docker(
                "inspect",
                "--type",
                "container",
                "-f",
                "{{.Id}}",
                tenant.container_name,
                timeout=10,
            )
            if inspect_code == 0:
                raise RuntimeError(
                    "could not remove the existing sandbox container"
                )
        else:
            await cls._wait_container_gone(tenant.container_name)
        cls._active_tenants.discard(tenant.container_name)
        cls._prepared_tenants.discard(tenant.container_name)
        cls._tenants.pop(tenant.container_name, None)
        cls._last_used_by_tenant.pop(tenant.container_name, None)
        cls._storage_state.pop(tenant.container_name, None)
        cls._idle_savers.pop(tenant.container_name, None)

    @classmethod
    async def discard_owner(cls, owner_id: str) -> int:
        """Privacy deletion: kill this user's sandboxes without saving /storage."""
        owner = str(owner_id)
        count = 0
        for tenant in [t for t in list(cls._tenants.values()) if t.owner_id == owner]:
            async with cls._lock_for(tenant):
                # Forget the merge state first so nothing gets written home.
                cls._storage_state.pop(tenant.container_name, None)
                cls._idle_savers.pop(tenant.container_name, None)
                cls._pins.pop(tenant.container_name, None)
                with contextlib.suppress(Exception):
                    await cls._destroy_container_unlocked(tenant)
                    count += 1
        return count

    @classmethod
    def pin(cls, message) -> str | None:
        """Keep this request's sandbox alive until unpin(); returns a token."""
        tenant = shell_tenant(message)
        if tenant is None:
            return None
        name = tenant.container_name
        cls._pins[name] = cls._pins.get(name, 0) + 1
        return name

    @classmethod
    def unpin(cls, name: str | None) -> None:
        if not name:
            return
        count = cls._pins.get(name, 0) - 1
        if count > 0:
            cls._pins[name] = count
            return
        cls._pins.pop(name, None)
        # Idle countdown starts when the job lets go, not when its last
        # shell call happened two hours ago.
        if name in cls._last_used_by_tenant:
            cls._last_used_by_tenant[name] = time.monotonic()
            cls._schedule_idle_reaper()

    @classmethod
    def _mark_used(cls, tenant: ShellTenant) -> None:
        cls._last_used_by_tenant[tenant.container_name] = time.monotonic()

    @classmethod
    def _schedule_idle_reaper(cls) -> None:
        task = cls._idle_reaper_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        cls._idle_reaper_task = loop.create_task(cls._idle_reaper_loop())

    @classmethod
    async def _idle_reaper_loop(cls) -> None:
        try:
            while True:
                idle = cls._idle_seconds()
                if idle <= 0 or not cls._last_used_by_tenant:
                    return
                now = time.monotonic()
                candidates = [
                    stamp for name, stamp in cls._last_used_by_tenant.items()
                    if not cls._pins.get(name)
                ]
                if not candidates:
                    # Everything left is pinned by a running job. Check back
                    # later instead of spinning on stamps we can't reap.
                    await asyncio.sleep(30.0)
                    continue
                remaining = min(idle - (now - stamp) for stamp in candidates)
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 30.0))
                    continue
                expired = [
                    name for name, stamp in cls._last_used_by_tenant.items()
                    if now - stamp >= idle
                    and name not in cls._active_tenants
                    and not cls._pins.get(name)
                ]
                reaped = False
                for name in expired:
                    tenant = cls._tenants.get(name)
                    if tenant is None:
                        # Stamp with no live container: drop it so the loop
                        # cannot spin on a name that will never be reaped.
                        cls._last_used_by_tenant.pop(name, None)
                        continue
                    async with cls._lock_for(tenant):
                        stamp = cls._last_used_by_tenant.get(name, 0.0)
                        if time.monotonic() - stamp < idle or cls._pins.get(name):
                            continue
                        # A background process may have written to /storage
                        # after the last command's save. Last chance.
                        saver = cls._idle_savers.get(name)
                        if saver is not None:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(saver(tenant), 60)
                        await cls._destroy_container_unlocked(tenant)
                        cls._last_used_by_tenant.pop(name, None)
                        cls._tenants.pop(name, None)
                        if not any(
                            known.owner_id == tenant.owner_id
                            for known in cls._tenants.values()
                        ) and tenant.owner_id not in cls._active_owners:
                            cls._user_slots.pop(tenant.owner_id, None)
                        logger.info("idle-recycled tenant shell %s", name)
                        reaped = True
                if not reaped:
                    # Nothing was reapable: every past-idle tenant is either
                    # currently running a command (its stamp is only refreshed
                    # in the command's finally) or was just handled above.
                    # Yield before re-checking — without an await here the loop
                    # spins and wedges the event loop.
                    await asyncio.sleep(1.0)
                if not cls._last_used_by_tenant:
                    return
        finally:
            if cls._idle_reaper_task is asyncio.current_task():
                cls._idle_reaper_task = None

    @classmethod
    async def shutdown_sandbox(cls) -> None:
        """Cancel active tenant containers during orderly service shutdown."""
        task = cls._idle_reaper_task
        cls._idle_reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for tenant in list(cls._tenants.values()):
            async with cls._lock_for(tenant):
                # Restarts wipe the guest; whatever a background process put
                # in /storage since the last command still goes home first.
                saver = cls._idle_savers.get(tenant.container_name)
                if saver is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(saver(tenant), 20)
                with contextlib.suppress(Exception):
                    await cls._destroy_container_unlocked(tenant)
        cls._active_tenants.clear()
        cls._active_owners.clear()
        cls._prepared_tenants.clear()

    async def _runtime_ready(self) -> None:
        if self._runtime() != "runsc":
            raise RuntimeError("shell disabled: required gVisor runtime 'runsc' is not configured")
        if not egress_policy_ready():
            raise RuntimeError(
                "shell disabled: host egress firewall is not provisioned; see docs/SHELL_SANDBOX.md"
            )
        if not resource_pool_ready():
            raise RuntimeError(
                "shell disabled: host resource pool is not provisioned; see docs/SHELL_SANDBOX.md"
            )
        if self._network() != "maxwell-shell-egress":
            raise RuntimeError("shell disabled: the filtered egress network is not configured")
        try:
            (stdout, stderr), code = await self._run_docker(
                "info", "--format", "{{json .Runtimes}}", timeout=10
            )
        except (FileNotFoundError, asyncio.TimeoutError) as exc:
            raise RuntimeError("shell disabled: Docker is unavailable") from exc
        if code != 0:
            raise RuntimeError("shell disabled: Docker could not report configured runtimes")
        try:
            runtimes = json.loads(stdout.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("shell disabled: Docker runtime configuration is unreadable") from exc
        safe_runtime, runtime_reason = validate_runsc_runtime(runtimes)
        if not safe_runtime:
            detail = stderr.decode(errors="replace").strip()
            raise RuntimeError(
                "shell disabled: gVisor runsc configuration is not safe ("
                + runtime_reason
                + ")"
                + (f" ({detail[:160]})" if detail else "")
            )
        (stdout, _stderr), code = await self._run_docker(
            "info", "--format", "{{.LiveRestoreEnabled}}", timeout=10
        )
        if code != 0 or stdout.decode(errors="replace").strip() != "false":
            raise RuntimeError(
                "shell disabled: Docker live-restore must be disabled so a daemon restart stops shell guests"
            )
        (stdout, _stderr), code = await self._run_docker(
            "info", "--format", "{{.CgroupDriver}}", timeout=10
        )
        if code != 0 or stdout.decode(errors="replace").strip() != "systemd":
            raise RuntimeError(
                "shell disabled: Docker must use systemd cgroups for the aggregate resource pool"
            )
        await self._recover_stale_sandboxes()

    @classmethod
    async def _recover_stale_sandboxes(cls) -> None:
        """Remove labeled containers left by a Maxwell process restart.

        Sandboxes are intentionally ephemeral across controller restarts so an
        orphaned process cannot continue consuming resources or retain a prior
        process's state. On a shared Docker daemon cleanup is scoped to this
        controller's own guests (MAXWELL_CONTROLLER_ID); the default controller
        also adopts unlabeled legacy sandboxes.
        """
        async with cls._recovery_lock:
            if cls._recovery_complete:
                return
            (stdout, stderr), code = await cls._run_docker(
                "ps", "-a", "--filter", "label=maxwell.shell.managed=true",
                "--format", '{{.ID}} {{.Label "maxwell.shell.controller"}}',
                timeout=15,
            )
            if code != 0:
                detail = stderr.decode(errors="replace").strip()
                raise RuntimeError(
                    "shell disabled: could not recover prior sandboxes"
                    + (f" ({detail[:120]})" if detail else "")
                )
            owner = controller_id()
            ids = []
            for line in stdout.decode(errors="replace").splitlines():
                container_id, _, label_owner = line.partition(" ")
                container_id = container_id.strip()
                if label_owner.strip() != owner:
                    continue
                if re.fullmatch(r"[a-f0-9]{12,64}", container_id):
                    ids.append(container_id)
            for offset in range(0, len(ids), 64):
                (_out, err), rm_code = await cls._run_docker(
                    "rm", "-f", *ids[offset : offset + 64], timeout=30
                )
                if rm_code != 0:
                    detail = err.decode(errors="replace").strip()
                    raise RuntimeError(
                        "shell disabled: could not clean prior sandboxes"
                        + (f" ({detail[:120]})" if detail else "")
                    )
            cls._recovery_complete = True

    async def _ensure_container(self, tenant: ShellTenant):
        await self._runtime_ready()
        async with self._state_lock:
            if tenant.container_name not in self._tenants:
                if len(self._tenants) >= self._MAX_CACHED_TENANTS:
                    raise RuntimeError("sandbox capacity is temporarily full")
                owner_count = sum(
                    known.owner_id == tenant.owner_id
                    for known in self._tenants.values()
                )
                if owner_count >= self._MAX_CACHED_PER_OWNER:
                    raise RuntimeError(
                        "this user's sandbox capacity is temporarily full"
                    )
                self._tenants[tenant.container_name] = tenant
        try:
            await self._ensure_container_impl(tenant)
        except Exception:
            if tenant.container_name not in self._prepared_tenants:
                async with self._state_lock:
                    self._tenants.pop(tenant.container_name, None)
                    self._last_used_by_tenant.pop(tenant.container_name, None)
            raise

    async def _ensure_container_impl(self, tenant: ShellTenant):
        storage_mb = await self._storage_mb(tenant)
        try:
            (stdout, _stderr), code = await self._run_docker(
                "inspect",
                "--type",
                "container",
                "-f",
                '{{.State.Running}} {{index .HostConfig "Runtime"}} '
                '{{index .Config.Labels "maxwell.shell.policy"}} '
                '{{index .Config.Labels "maxwell.shell.controller"}}'
                # Tab-separated so "<no value>" (which has a space) can't
                # shift the fields in front of it.
                '\t{{index .Config.Labels "maxwell.shell.storage"}}',
                tenant.container_name,
                timeout=10,
            )
            if code == 0:
                text, _tab, storage_label = stdout.decode(errors="replace").strip().partition("\t")
                storage_label = storage_label.strip()
                current_mb = int(storage_label) if storage_label.isdigit() else 0
                parts = text.strip().split(None, 3)
                running = (parts[0] if parts else "").lower() == "true"
                runtime = parts[1] if len(parts) > 1 else ""
                policy = parts[2] if len(parts) > 2 else ""
                # Docker renders a missing label as "<no value>"; only a real,
                # different value means the container belongs to someone else.
                controller = parts[3].strip() if len(parts) > 3 else ""
                if controller in {"", "<no value>"}:
                    controller = ""
                if controller != controller_id():
                    raise RuntimeError(
                        "sandbox name collision with another controller's container"
                    )
                if (
                    running
                    and runtime == "runsc"
                    and policy == "v2"
                    and tenant.container_name in self._prepared_tenants
                    and current_mb == storage_mb
                ):
                    self._storage_state.setdefault(tenant.container_name, {"mb": current_mb})
                    return
                if current_mb and current_mb != storage_mb and tenant.container_name in self._prepared_tenants:
                    # Plan changed under a live guest. Save what it has
                    # before it's replaced with the right-sized /storage.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self._storage_save(tenant), 60)
                await self._destroy_container_unlocked(tenant)
        except FileNotFoundError as exc:
            raise RuntimeError("docker is not installed or not on PATH") from exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError("docker did not respond while checking sandbox") from exc

        try:
            await _ensure_sandbox_image(self.IMAGE_NAME)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"could not prepare sandbox image: {exc}") from exc

        run_args = self._sandbox_run_args(tenant, storage_mb)
        (_stdout, stderr), run_code = await self._run_docker(*run_args, timeout=30)
        if run_code != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "docker run failed"
            )
        self._active_tenants.add(tenant.container_name)
        self._prepared_tenants.add(tenant.container_name)
        self._storage_state[tenant.container_name] = {"mb": storage_mb}

    def _sandbox_run_args(self, tenant: ShellTenant, storage_mb: int = 0) -> list[str]:
        return docker_run_args(
            container_name=tenant.container_name,
            image=self.IMAGE_NAME,
            runtime=self._runtime(),
            network=self._network(),
            storage_mb=storage_mb,
        )

    # ---- persistent /storage -------------------------------------------

    def _user_storage(self):
        return getattr(self.bot, "_user_storage", None) if self.bot is not None else None

    async def _storage_mb(self, tenant: ShellTenant) -> int:
        storage = self._user_storage()
        if storage is None:
            return 0
        try:
            quota = await asyncio.to_thread(storage.quota, tenant.owner_id)
        except Exception:
            logger.warning("storage quota lookup failed", exc_info=True)
            return 0
        return max(1, quota // (1024 * 1024)) if quota > 0 else 0

    async def _guest_exec(
        self, tenant: ShellTenant, script: str, *, stdin_file=None, stdout_file=None,
        stdout_limit: int = 0, timeout: float = 60,
    ) -> tuple[int, bytes, bytes]:
        """Run a trusted helper script in the guest; never user input."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", *(["-i"] if stdin_file is not None else []),
            "--user", "root", tenant.container_name, "sh", "-c", script,
            stdin=asyncio.subprocess.PIPE if stdin_file is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out = bytearray()
        received = 0

        async def feed():
            if stdin_file is None:
                return
            try:
                while True:
                    chunk = await asyncio.to_thread(stdin_file.read, 1024 * 1024)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        async def pump():
            nonlocal received
            while True:
                chunk = await proc.stdout.read(1024 * 1024)
                if not chunk:
                    return
                received += len(chunk)
                if stdout_limit and received > stdout_limit:
                    raise OverflowError("guest output exceeded its limit")
                if stdout_file is not None:
                    await asyncio.to_thread(stdout_file.write, chunk)
                else:
                    out.extend(chunk)

        try:
            _fed, _pumped, err, _waited = await asyncio.wait_for(
                asyncio.gather(feed(), pump(), proc.stderr.read(), proc.wait()), timeout
            )
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        return proc.returncode, bytes(out), err

    _FINGERPRINT = (
        "find /storage -mindepth 1 -printf '%P\t%y\t%s\t%T@\n' 2>/dev/null "
        "| LC_ALL=C sort | sha256sum | cut -c1-64"
    )

    async def _storage_fingerprint(self, tenant: ShellTenant) -> str | None:
        try:
            code, out, _err = await self._guest_exec(tenant, self._FINGERPRINT, timeout=30)
        except Exception:
            return None
        value = out.decode(errors="replace").strip()
        return value if code == 0 and len(value) == 64 else None

    async def _storage_restore(self, tenant: ShellTenant) -> None:
        """Load the host copy into the guest's /storage if it's stale."""
        name = tenant.container_name
        state = self._storage_state.get(name) or {}
        storage = self._user_storage()
        if storage is None or not state.get("mb"):
            return
        owner = tenant.owner_id
        if "base" in state and not state.get("broken"):
            # A background process may have written since the last save, and
            # the restore below starts with a wipe. Merge those writes first.
            note = await self._storage_save(tenant)
            if note:
                self._storage_notes[name] = note
            state = self._storage_state.get(name) or state
        generation = await asyncio.to_thread(storage.generation, owner)
        if state.get("gen") == generation and "base" in state:
            return
        spool, manifest, generation = await asyncio.to_thread(storage.export_tar, owner)
        try:
            code, _out, err = await self._guest_exec(
                tenant,
                "find /storage -mindepth 1 -delete && tar -x -C /storage --no-same-owner -f -",
                stdin_file=spool,
                timeout=120,
            )
        except Exception as exc:
            code, err = 1, str(type(exc).__name__).encode()
        finally:
            spool.close()
        if code != 0:
            # Partially restored is worse than empty: a save-back from here
            # would read every missing file as "the user deleted it". Freeze.
            used, _files = await asyncio.to_thread(storage.usage, owner)
            self._storage_state[name] = {"mb": state["mb"], "broken": True}
            self._storage_notes[name] = (
                f"[storage] /storage could not be loaded ({used / 1048576:.1f} MB stored, "
                f"{state['mb']} MB allowed), so changes to it this session will not be saved. "
                "Free space with the storage tool or the dashboard."
            )
            logger.warning("storage restore failed for %s: %s", name, err.decode(errors="replace")[:200])
            return
        fingerprint = await self._storage_fingerprint(tenant)
        self._storage_state[name] = {
            "mb": state["mb"], "gen": generation, "base": manifest, "fingerprint": fingerprint,
        }

    async def _storage_save(self, tenant: ShellTenant) -> str:
        """Merge the guest's /storage back to the host. Returns a note or ''."""
        name = tenant.container_name
        state = self._storage_state.get(name) or {}
        storage = self._user_storage()
        if storage is None or not state.get("mb") or state.get("broken") or "base" not in state:
            return ""
        fingerprint = await self._storage_fingerprint(tenant)
        if fingerprint is None or fingerprint == state.get("fingerprint"):
            return ""
        limit = state["mb"] * 1024 * 1024 * 2 + 16 * 1024 * 1024
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
            try:
                code, _out, err = await self._guest_exec(
                    tenant, "tar -c -C /storage -f - .", stdout_file=spool,
                    stdout_limit=limit, timeout=120,
                )
            except OverflowError:
                state["fingerprint"] = fingerprint
                return "[storage] /storage is larger than its quota; changes were not saved."
            if code != 0:
                logger.warning("storage export failed for %s: %s", name, err.decode(errors="replace")[:200])
                return "[storage] could not read /storage; changes were not saved this time."
            spool.seek(0)
            result = await asyncio.to_thread(
                storage.import_tar, tenant.owner_id, spool, state["base"],
                base_generation=state.get("gen", -1),
            )
        # Either way, don't re-export the same tree after every command.
        state["fingerprint"] = fingerprint
        if result.error:
            return f"[storage] {result.error}"
        if result.clean and result.manifest is not None:
            state["gen"], state["base"] = result.generation, result.manifest
        else:
            # Someone else wrote meanwhile; reload the merged tree next time.
            state["gen"] = -1
        if result.skipped:
            shown = ", ".join(result.skipped[:5])
            more = f" and {len(result.skipped) - 5} more" if len(result.skipped) > 5 else ""
            return f"[storage] not saved (symlinks/special files are not kept): {shown}{more}"
        return ""

    async def persist_to_storage(self, message, paths: list[str], folder: str) -> list[str]:
        """Copy sandbox files into /storage/<folder>/ and save. For jobs.

        Returns the /storage paths that exist afterwards. Quietly does
        nothing when this user has no persistent storage or no live sandbox.
        """
        tenant = shell_tenant(message)
        if tenant is None or tenant.container_name not in self._prepared_tenants:
            return []
        state = self._storage_state.get(tenant.container_name) or {}
        if not state.get("mb") or state.get("broken"):
            return []
        sources = []
        for raw in paths:
            rel = self._workspace_relative_path(raw)
            if rel is not None:
                sources.append("/workspace/" + rel)
        if not sources or not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", folder or ""):
            return []
        dest = "/storage/" + folder.strip("/")
        script = "mkdir -p " + shlex.quote(dest) + " && " + " ; ".join(
            f"[ -f {shlex.quote(src)} ] && [ ! -L {shlex.quote(src)} ] && "
            f"cp -f -- {shlex.quote(src)} {shlex.quote(dest)}/ && echo {shlex.quote(src)}"
            for src in sources
        )
        async with self._user_slot(tenant):
            async with self._lock_for(tenant):
                if tenant.container_name not in self._prepared_tenants:
                    return []
                try:
                    await self._storage_restore(tenant)
                    _code, out, _err = await self._guest_exec(tenant, script, timeout=120)
                    note = await self._storage_save(tenant)
                except Exception:
                    logger.warning("job artifact persistence failed", exc_info=True)
                    return []
                if note:
                    logger.info("job artifact persistence: %s", note)
        copied = [line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()]
        wanted = [dest + "/" + os.path.basename(src) for src in copied]
        # The guest saying "copied" means nothing if the merge refused it
        # (quota, concurrent edit). Only report what the host really has.
        storage = self._user_storage()
        kept = []
        for path in wanted:
            with contextlib.suppress(Exception):
                if await asyncio.to_thread(storage.file_path, tenant.owner_id, path) is not None:
                    kept.append(path)
        return kept

    @staticmethod
    def _command_arg(command: str | None = None, **kwargs) -> str | None:
        """Pick the command string out of native-tool args.

        Models frequently send `cmd` (and sometimes `script`/`code`) instead
        of `command`. Accept those aliases so a valid heredoc is not rejected
        as an empty command.
        """
        if command is not None and str(command).strip():
            return command
        for key in ("cmd", "script", "code"):
            val = kwargs.get(key)
            if val is not None and str(val).strip():
                return val
        return command

    def _normalize_command(self, command: str | None) -> str:
        raw = str(command or "").strip()
        if not raw:
            return ""
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")

        # Models wrap the command in a markdown fence, or copy the `$ `
        # prompt from the channel echo of a previous shell call.
        fence = re.match(
            r"^```(?:bash|sh|shell|zsh|python|py)?[ \t]*\n(.*)\n```[ \t]*$",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        if fence:
            raw = fence.group(1).strip()
        if raw.startswith("$"):
            raw = re.sub(r"^\$[ \t]+", "", raw)

        # If the model leaked a tool call payload, try to recover a literal command from backticks.
        if "<tool:" in raw.lower():
            m = re.search(r"`([^`]+)`", raw)
            if m:
                return m.group(1).strip()
            return ""
        return raw

    def _validate_command(self, command: str) -> str | None:
        """Validate input size and heredoc shape before sandbox execution."""
        if not command:
            return "empty command"
        max_len = self._max_command_length()
        if max_len and len(command) > max_len:
            return f"command too long (max {max_len} chars)"
        # Multi-line commands & heredocs are allowed.
        if "\n" in command:
            hint = _unterminated_heredoc_error(command)
            if hint:
                return "heredoc error — " + hint
        non_heredoc = _strip_heredoc_blocks(command)
        if any(ord(c) < 32 and c not in ("\t", "\n", "\r") for c in non_heredoc):
            return "control characters are not allowed in shell commands"
        return None

    _PROGRESS_TICK_SECONDS = 0.8

    async def _run_shell_command(self, command: str, tenant: ShellTenant, on_progress=None):
        # One user cannot consume the global pool by issuing commands against
        # several guild/private scopes at once.
        async with self._user_slot(tenant):
            return await self._run_shell_command_inner(
                command, tenant, on_progress=on_progress
            )

    async def _run_shell_command_inner(
        self, command: str, tenant: ShellTenant, on_progress=None
    ):
        sanitized = self._normalize_command(command)
        validation_error = self._validate_command(sanitized)
        if validation_error:
            raise RuntimeError(validation_error)
        if not sanitized:
            raise RuntimeError("empty command")
        timeout = await self._command_timeout(tenant)
        async with self._global_slots:
            async with self._lock_for(tenant):
                await self._ensure_container(tenant)
                self._active_tenants.add(tenant.container_name)
                self._active_owners.add(tenant.owner_id)
                type(self)._idle_savers[tenant.container_name] = self._storage_save
                try:
                    try:
                        await self._storage_restore(tenant)
                    except Exception:
                        # Storage trouble must not take the shell down with it.
                        logger.warning("storage restore failed", exc_info=True)
                    exec_token = f"maxwell-exec-{uuid.uuid4().hex}"
                    pid_file = f"/tmp/{exec_token}.pid"
                    # Run the user's shell in its own session/process group and leave
                    # its leader PID in the container. Killing only the local
                    # `docker exec` client does not kill a child command; pipelines,
                    # background jobs, and `sleep` would otherwise survive every
                    # timeout and accumulate in the persistent sandbox.
                    script_file = pid_file + ".sh"
                    wrapped = (
                        f"echo $$ > {shlex.quote(pid_file)}\n"
                        f"rm -f {shlex.quote(script_file)}\n"
                        f"trap 'rm -f {shlex.quote(pid_file)} {shlex.quote(script_file)}' EXIT\n{sanitized}\n"
                    )
                    bootstrap = (
                        f"umask 077; cat > {shlex.quote(script_file)} && "
                        f"exec setsid --wait bash -l {shlex.quote(script_file)} </dev/null"
                    )
                    proc = await asyncio.create_subprocess_exec(
                        "docker",
                        "exec",
                        "-i",
                        "--workdir",
                        "/workspace",
                        "--user",
                        "root",
                        tenant.container_name,
                        "bash",
                        "-lc",
                        bootstrap,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout_buf = bytearray()
                    stderr_buf = bytearray()
                    started = time.monotonic()
                    last_tick = 0.0

                    async def _emit(force: bool = False) -> None:
                        nonlocal last_tick
                        if on_progress is None:
                            return
                        now = time.monotonic()
                        if (
                            not force
                            and last_tick
                            and now - last_tick < self._PROGRESS_TICK_SECONDS
                        ):
                            return
                        last_tick = now
                        with contextlib.suppress(Exception):
                            await on_progress(
                                bytes(stdout_buf),
                                bytes(stderr_buf),
                                now - started,
                            )

                    async def _pump(stream, buf: bytearray) -> None:
                        if stream is None:
                            return
                        while True:
                            chunk = await stream.read(4096)
                            if not chunk:
                                break
                            buf.extend(chunk)
                            await _emit()

                    async def _feed() -> None:
                        # Stdin avoids the OS's per-argument size limit for
                        # large scripts, without changing the sandbox boundary.
                        try:
                            proc.stdin.write(wrapped.encode())
                            await proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        finally:
                            proc.stdin.close()

                    async def _heartbeat() -> None:
                        try:
                            while True:
                                await asyncio.sleep(self._PROGRESS_TICK_SECONDS)
                                if proc.returncode is not None:
                                    return
                                await _emit()
                        except asyncio.CancelledError:
                            return

                    beat = asyncio.create_task(_heartbeat())
                    try:
                        await _emit(force=True)
                        await asyncio.wait_for(
                            asyncio.gather(
                                _feed(),
                                _pump(proc.stdout, stdout_buf),
                                _pump(proc.stderr, stderr_buf),
                                proc.wait(),
                            ),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        await self._kill_container_exec(tenant, pid_file)
                        cleanup = asyncio.create_task(
                            self._save_then_destroy(tenant)
                        )
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(cleanup)
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        await proc.wait()
                        raise
                    except asyncio.CancelledError:
                        # Destroying the whole tenant container guarantees that
                        # disowned/session-escaped descendants cannot outlive cancel.
                        cleanup = asyncio.create_task(
                            self._save_then_destroy(tenant)
                        )
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(cleanup)
                        if proc.returncode is None:
                            with contextlib.suppress(ProcessLookupError):
                                proc.kill()
                            await proc.wait()
                        raise
                    finally:
                        beat.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await beat
                        # Belt-and-suspenders: ensure no zombie if communicate didn't finish.
                        if proc.returncode is None:
                            try:
                                await self._kill_container_exec(tenant, pid_file)
                                proc.kill()
                                await proc.wait()
                            except Exception as e:
                                # Usually means the process already exited.
                                logger.debug("shell zombie cleanup: %s", e)
                    try:
                        note = await self._storage_save(tenant)
                    except Exception:
                        logger.warning("storage save failed", exc_info=True)
                        note = "[storage] saving /storage failed; changes may not be kept."
                    if note:
                        type(self)._storage_notes[tenant.container_name] = note
                    return bytes(stdout_buf), bytes(stderr_buf), proc.returncode
                finally:
                    type(self)._active_tenants.discard(tenant.container_name)
                    if not any(
                        known.owner_id == tenant.owner_id
                        and known.container_name in type(self)._active_tenants
                        for known in type(self)._tenants.values()
                    ):
                        type(self)._active_owners.discard(tenant.owner_id)
                    type(self)._mark_used(tenant)
                    type(self)._schedule_idle_reaper()

    async def _save_then_destroy(self, tenant: ShellTenant) -> None:
        """Timeout/cancel path: keep /storage writes, then kill the guest."""
        with contextlib.suppress(Exception):
            note = await asyncio.wait_for(self._storage_save(tenant), 60)
            if note:
                type(self)._storage_notes[tenant.container_name] = note
        await self._destroy_container_unlocked(tenant)

    async def _kill_container_exec(self, tenant: ShellTenant, pid_file: str) -> None:
        """Terminate the timed-out command, not just its docker client."""
        quoted = shlex.quote(pid_file)
        cleanup = (
            f"pid=$(cat {quoted} 2>/dev/null); "
            'case "$pid" in '
            "''|*[!0-9]*) ;; "
            f"*) kill -TERM -- -$pid 2>/dev/null; sleep 0.2; "
            f"kill -KILL -- -$pid 2>/dev/null; rm -f {quoted} ;; "
            "esac"
        )
        with contextlib.suppress(Exception):
            await self._run_docker(
                "exec",
                "--user",
                "root",
                tenant.container_name,
                "bash",
                "-lc",
                cleanup,
                timeout=10,
            )

    async def execute(
        self,
        message: Message,
        command: str | None = None,
        files: str | None = None,
        **kwargs,
    ) -> str:
        normalized = self._normalize_command(self._command_arg(command, **kwargs))
        if not normalized:
            return "Error: command is required (tool-call markup was detected or command was empty)"

        # No whitelist: any user in an allowed channel can run shell. The
        # sandbox is the security boundary (root inside container, but no
        # host / mount, no host net, no docker socket by default). Idle
        # recycle drops leftover processes/packages after MAXWELL_SHELL_IDLE_SECONDS.

        tenant = shell_tenant(message)
        if tenant is None:
            return "Error: shell needs an authenticated Discord user and request scope"

        # The dispatcher owns the temporary tool status. Shell must not post
        # another persistent message for each command in the same turn.
        try:
            stdout, stderr, exit_code = await self._run_shell_command(
                normalized, tenant
            )
        except asyncio.TimeoutError:
            limit = await self._command_timeout(tenant)
            note = type(self)._storage_notes.pop(tenant.container_name, "")
            saved = (
                " Files saved in /storage were kept."
                if await self._storage_mb(tenant) else ""
            )
            return (
                f"Error: Command timed out after {limit}s and the sandbox was reset "
                f"(/workspace wiped).{saved} Split long work into shorter commands or run it "
                "in the background with nohup and poll." + (f"\n{note}" if note else "")
            )
        except Exception as e:
            logger.warning("shell execution failed (%s)", type(e).__name__)
            return (
                "Error: isolated shell is unavailable. Check that Docker has "
                "gVisor runsc, the filtered egress policy, and bounded storage configured."
            )

        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        combined = ""
        if out.strip():
            combined += out.strip()
        if err.strip():
            if combined:
                combined += "\n"
            combined += f"[stderr] {err.strip()}"
        if exit_code != 0:
            combined += f"\n[exit code: {exit_code}]"

        result = combined if combined else "(command produced no output)"
        note = type(self)._storage_notes.pop(tenant.container_name, "")
        if note:
            result += "\n" + note

        # Send requested files from the container
        if files:
            file_paths = self._parse_file_list(files)
            sent_files = []
            for fpath in file_paths:
                sent = await self._send_container_file(message, fpath)
                if sent:
                    sent_files.append(sent)
            if sent_files:
                result += f"\nSent files: {', '.join(sent_files)}"

        return result

    @staticmethod
    def _parse_file_list(files: str) -> list[str]:
        raw = str(files or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(f).strip() for f in parsed if str(f).strip()]
            if isinstance(parsed, str):
                return [parsed.strip()] if parsed.strip() else []
        except (json.JSONDecodeError, ValueError):
            pass
        # Fall back to comma-separated
        return [f.strip() for f in raw.split(",") if f.strip()]

    @staticmethod
    def _workspace_relative_path(path: str) -> str | None:
        raw = str(path or "").strip()
        if raw.startswith("/workspace/"):
            raw = raw[len("/workspace/"):]
        elif raw == "/workspace":
            return None
        elif raw.startswith("workspace/"):
            raw = raw[len("workspace/"):]
        elif raw.startswith("/"):
            return None
        parts = raw.replace("\\", "/").split("/")
        if not raw or any(part in {"", ".", ".."} for part in parts):
            return None
        return "/".join(parts)

    async def read_workspace_file(
        self, message: Message, path: str, *, max_size: int = 25 * 1024 * 1024
    ) -> tuple[bytes | None, str | None, str | None]:
        """Read a bounded regular file without host-side symlink races."""
        tenant = shell_tenant(message)
        raw = str(path or "").strip()
        if raw == "/storage" or raw.startswith("/storage/"):
            # The host copy is authoritative and outlives the sandbox, which
            # is the whole point: no live container needed.
            storage = self._user_storage()
            if tenant is None or storage is None:
                return None, None, "persistent storage is not available"
            try:
                limit = max(1, min(int(max_size), 25 * 1024 * 1024))
            except (TypeError, ValueError):
                limit = 25 * 1024 * 1024
            from plugins.user_storage.store import StorageError

            try:
                blob = await asyncio.to_thread(storage.read, tenant.owner_id, raw, max_size=limit)
            except StorageError as exc:
                return None, None, str(exc)
            return blob, os.path.basename(raw.rstrip("/")), None
        relative = self._workspace_relative_path(path)
        if tenant is None or relative is None:
            return None, None, "path must identify a file inside /workspace or /storage"
        if tenant.container_name not in self._prepared_tenants:
            return None, None, "this user's shell workspace is not active"
        try:
            limit = max(1, min(int(max_size), 25 * 1024 * 1024))
        except (TypeError, ValueError):
            limit = 25 * 1024 * 1024
        safe_reader = r'''import os,stat,sys
rel=sys.argv[1]
limit=int(sys.argv[2])
parts=rel.split("/")
if not parts or any(p in ("", ".", "..") for p in parts):
    sys.exit(2)
root=os.open("/workspace", os.O_RDONLY|os.O_DIRECTORY)
parent=root
try:
    for part in parts[:-1]:
        child=os.open(part, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent)
        if parent != root:
            os.close(parent)
        parent=child
    target=os.open(parts[-1], os.O_RDONLY|os.O_NOFOLLOW, dir_fd=parent)
    try:
        info=os.fstat(target)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            sys.exit(3)
        data=bytearray()
        while len(data) <= limit:
            chunk=os.read(target, min(65536, limit+1-len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            sys.exit(3)
        sys.stdout.buffer.write(data)
    finally:
        os.close(target)
finally:
    if parent != root:
        os.close(parent)
    os.close(root)
'''
        try:
            async with self._lock_for(tenant):
                if tenant.container_name not in self._prepared_tenants:
                    return None, None, "this user's shell workspace is not active"
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    "exec",
                    "--user",
                    "root",
                    tenant.container_name,
                    "python3",
                    "-c",
                    safe_reader,
                    relative,
                    str(limit),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    blob, _stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=15
                    )
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
                    return None, None, "workspace read timed out"
                if proc.returncode != 0:
                    if proc.returncode == 3:
                        return None, None, f"file exceeds the {limit}-byte limit or is not a regular file"
                    return None, None, "file not found or path contains a symlink"
                if len(blob) > limit:
                    return None, None, f"file exceeds the {limit}-byte limit"
                return blob, os.path.basename(relative), None
        except (OSError, asyncio.TimeoutError) as exc:
            return None, None, type(exc).__name__

    async def _send_container_file(self, message: Message, rel_path: str) -> str | None:
        blob, basename, error = await self.read_workspace_file(
            message, rel_path, max_size=10 * 1024 * 1024
        )
        if blob is None or basename is None:
            logger.info("Shell file export rejected (%s)", error or "unknown")
            return None
        filename = _safe_attachment_filename(basename, default="file")
        self._signal_streaming(message)
        await message.channel.send(file=File(BytesIO(blob), filename=filename))
        return filename
