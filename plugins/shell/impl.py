"""Tool implementations for the shell plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from typing import ClassVar

from tooling import helpers as _helpers
from tools import Tool
from plugins.shell.isolation import (
    ShellTenant,
    docker_run_args,
    egress_policy_ready,
    resource_pool_ready,
    shell_tenant,
    validate_runsc_runtime,
)

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
    _SANDBOX_INIT = "6"

    # Limits are hard bounded. Operator configuration can lower these values,
    # but cannot disable the cap.
    _MAX_OUTPUT_DEFAULT = 100_000
    _MAX_COMMAND_LENGTH_DEFAULT = 65_536
    # Channel post cap. Captured stdout can be 100k for the model, but posting
    # that as Discord ```ansi``` chunks floods the chat. Visible dump is ~300
    # chars (one short codeblock); the LLM still gets the longer capture.
    _CHANNEL_MAX_CHARS_DEFAULT = 300
    _CHANNEL_MAX_CHUNKS = 1

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

    @classmethod
    def _max_output(cls) -> int:
        """Captured stdout+stderr cap, bounded at 1 MiB."""
        raw = os.environ.get("MAXWELL_SHELL_MAX_OUTPUT", "").strip()
        if not raw:
            return cls._MAX_OUTPUT_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._MAX_OUTPUT_DEFAULT
        return max(1, min(v, 1_000_000))

    @classmethod
    def _max_command_length(cls) -> int:
        """Max chars in a single shell command, bounded at 64 KiB."""
        raw = os.environ.get("MAXWELL_SHELL_MAX_COMMAND_LENGTH", "").strip()
        if not raw:
            return cls._MAX_COMMAND_LENGTH_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._MAX_COMMAND_LENGTH_DEFAULT
        return max(1, min(v, 65_536))

    @classmethod
    def _channel_max_chars(cls) -> int:
        """Max chars posted to the chat for one shell call. 0 = unlimited."""
        raw = os.environ.get("MAXWELL_SHELL_CHANNEL_MAX_CHARS", "").strip()
        if not raw:
            return cls._CHANNEL_MAX_CHARS_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._CHANNEL_MAX_CHARS_DEFAULT
        return max(1, min(v, 1_900))

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

    @staticmethod
    def _full_host_access() -> bool:
        """Retained for compatibility; public full-host mode is removed."""
        return False

    def get_description(self):
        # Surface live limits so the model doesn't have to guess. Pulled at
        # description-build time, which happens per-turn on tool registration.
        max_out = self._max_output()
        max_cmd = self._max_command_length()
        to = self._timeout_seconds()
        max_out_str = f"{max_out:,} chars"
        max_cmd_str = f"{max_cmd:,} chars"
        chan = self._channel_max_chars()
        chan_str = "unlimited" if chan == 0 else f"{chan} chars"
        idle = self._idle_seconds()
        if idle:
            persist_note = (
                f"Sandbox and /workspace are wiped after {idle}s idle "
                "and recreated on the next call."
            )
        else:
            persist_note = "Container persists across calls."
        limits_note = (
            f"Limits: command <= {max_cmd_str}, output <= {max_out_str}, "
            f"channel preview <= {chan_str}, timeout {to}s. {persist_note}"
        )
        how = (
            "To write a file, put the redirect on the opener line: "
            "`cat << 'EOF' > path/file.py` then the body then a line containing "
            "only EOF. `cmd` is an alias for `command`. Do not prefix `$ ` or "
            "wrap the command in a markdown fence. Attach outputs with files= "
            "(comma-separated paths under /workspace)."
        )
        return (
            "Run bash -lc as root inside this user's gVisor sandbox (workdir "
            "/workspace). Root is confined by runsc; it has no host capabilities "
            "and receives only a small in-guest capability set. Params: command (required), files "
            "(optional paths under /workspace to attach privately or publicly "
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
        cls._tenants[tenant.container_name] = tenant

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
                remaining = min(
                    idle - (now - stamp)
                    for stamp in cls._last_used_by_tenant.values()
                )
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 30.0))
                    continue
                expired = [
                    name for name, stamp in cls._last_used_by_tenant.items()
                    if now - stamp >= idle and name not in cls._active_tenants
                ]
                for name in expired:
                    tenant = cls._tenants.get(name)
                    if tenant is None:
                        continue
                    async with cls._lock_for(tenant):
                        stamp = cls._last_used_by_tenant.get(name, 0.0)
                        if time.monotonic() - stamp < idle:
                            continue
                        await cls._destroy_container_unlocked(tenant)
                        cls._last_used_by_tenant.pop(name, None)
                        cls._tenants.pop(name, None)
                        if not any(
                            known.owner_id == tenant.owner_id
                            for known in cls._tenants.values()
                        ) and tenant.owner_id not in cls._active_owners:
                            cls._user_slots.pop(tenant.owner_id, None)
                        logger.info("idle-recycled tenant shell %s", name)
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

        The deployment runs one Maxwell controller. Sandboxes are intentionally
        ephemeral across controller restarts so an orphaned process cannot
        continue consuming resources or retain a prior process's state.
        """
        async with cls._recovery_lock:
            if cls._recovery_complete:
                return
            (stdout, stderr), code = await cls._run_docker(
                "ps", "-aq", "--filter", "label=maxwell.shell.managed=true",
                timeout=15,
            )
            if code != 0:
                detail = stderr.decode(errors="replace").strip()
                raise RuntimeError(
                    "shell disabled: could not recover prior sandboxes"
                    + (f" ({detail[:120]})" if detail else "")
                )
            ids = [
                value.strip()
                for value in stdout.decode(errors="replace").splitlines()
                if re.fullmatch(r"[a-f0-9]{12,64}", value.strip())
            ]
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
        try:
            (stdout, _stderr), code = await self._run_docker(
                "inspect",
                "--type",
                "container",
                "-f",
                '{{.State.Running}} {{index .HostConfig "Runtime"}} '
                '{{index .Config.Labels "maxwell.shell.policy"}}',
                tenant.container_name,
                timeout=10,
            )
            if code == 0:
                parts = stdout.decode(errors="replace").strip().split(None, 3)
                running = (parts[0] if parts else "").lower() == "true"
                runtime = parts[1] if len(parts) > 1 else ""
                policy = parts[2] if len(parts) > 2 else ""
                if (
                    running
                    and runtime == "runsc"
                    and policy == "v2"
                    and tenant.container_name in self._prepared_tenants
                ):
                    return
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

        run_args = self._sandbox_run_args(tenant)
        (_stdout, stderr), run_code = await self._run_docker(*run_args, timeout=30)
        if run_code != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "docker run failed"
            )
        self._active_tenants.add(tenant.container_name)
        self._prepared_tenants.add(tenant.container_name)

    def _sandbox_run_args(self, tenant: ShellTenant) -> list[str]:
        return docker_run_args(
            container_name=tenant.container_name,
            image=self.IMAGE_NAME,
            runtime=self._runtime(),
            network=self._network(),
        )

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
        if len(command) > max_len:
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
        async with self._global_slots:
            async with self._lock_for(tenant):
                await self._ensure_container(tenant)
                self._active_tenants.add(tenant.container_name)
                self._active_owners.add(tenant.owner_id)
                try:
                    exec_token = f"maxwell-exec-{uuid.uuid4().hex}"
                    pid_file = f"/tmp/{exec_token}.pid"
                    # Run the user's shell in its own session/process group and leave
                    # its leader PID in the container. Killing only the local
                    # `docker exec` client does not kill a child command; pipelines,
                    # background jobs, and `sleep` would otherwise survive every
                    # timeout and accumulate in the persistent sandbox.
                    inner = f"trap 'rm -f {shlex.quote(pid_file)}' EXIT; {sanitized}"
                    wrapped = (
                        f"echo $$ > {shlex.quote(pid_file)}; exec bash -lc {shlex.quote(inner)}"
                    )
                    proc = await asyncio.create_subprocess_exec(
                        "docker",
                        "exec",
                        "--workdir",
                        "/workspace",
                        "--user",
                        "root",
                        tenant.container_name,
                        "setsid",
                        "--wait",
                        "bash",
                        "-lc",
                        wrapped,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout_buf = bytearray()
                    stderr_buf = bytearray()
                    max_output = self._max_output()
                    captured = 0
                    output_truncated = False
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
                            nonlocal captured, output_truncated
                            remaining = max_output - captured
                            if remaining > 0:
                                kept = chunk[:remaining]
                                buf.extend(kept)
                                captured += len(kept)
                            if len(kept if remaining > 0 else b"") < len(chunk):
                                output_truncated = True
                            await _emit()

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
                                _pump(proc.stdout, stdout_buf),
                                _pump(proc.stderr, stderr_buf),
                                proc.wait(),
                            ),
                            timeout=self._timeout_seconds(),
                        )
                    except asyncio.TimeoutError:
                        await self._kill_container_exec(tenant, pid_file)
                        cleanup = asyncio.create_task(
                            self._destroy_container_unlocked(tenant)
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
                            self._destroy_container_unlocked(tenant)
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
                    if output_truncated:
                        stderr_buf.extend(b"\n[output truncated at MAXWELL_SHELL_MAX_OUTPUT]")
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

    def _shell_echo_text(self, command: str, *suffixes: str) -> str:
        """Build the body for a ```ansi block: a (truncated) command echo + suffix lines.

        The command can be a long multi-line script; echoing it verbatim blows
        past Discord's 2000-char limit once wrapped in a codeblock. Cap the
        echo so the actual error/output — the useful part — always fits.
        """
        max_echo = 80
        echo = (
            command
            if len(command) <= max_echo
            else command[:max_echo] + " …(truncated)"
        )
        parts = [f"$ {echo}"]
        parts.extend(s for s in suffixes if s)
        return "\n".join(parts)

    def _shell_running_text(
        self, command: str, stdout: bytes, stderr: bytes, elapsed: float
    ) -> str:
        secs = int(elapsed)
        status = "… running" if secs < 1 else f"… running {secs}s"
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        body = out
        if err:
            body = f"{body}\n[stderr] {err}" if body else f"[stderr] {err}"
        if body:
            return self._shell_echo_text(command, status, body)
        return self._shell_echo_text(command, status)

    def _truncate_shell_preview(self, text: str, limit: int) -> str:
        """Keep `$ cmd` / running status plus the newest tail when truncating."""
        notice = "\n... (truncated for channel)"
        if limit <= 0 or len(text) <= limit:
            return text
        keep = max(0, limit - len(notice))
        if keep <= 0:
            return notice[-limit:]
        first_nl = text.find("\n")
        header_end = first_nl if first_nl >= 0 else min(len(text), keep)
        if first_nl >= 0:
            second_nl = text.find("\n", first_nl + 1)
            second = text[first_nl + 1 : second_nl if second_nl >= 0 else len(text)]
            if second.startswith("… "):
                header_end = second_nl if second_nl >= 0 else len(text)
        header = text[:header_end]
        body = text[header_end:]
        if len(header) >= keep:
            return header[:keep] + notice
        room = keep - len(header)
        if len(body) <= room:
            return header + body
        return header + notice + body[-room:]

    def _format_ansi_message(self, text: str) -> str:
        """Format shell status message under Discord's 2000 cap."""
        return str(text or "")[:1990]

    async def _flush_shell_progress_unlocked(
        self, message: Message, sess: _ShellProgressTurn
    ) -> None:
        rendered = "\n\n".join(part for part in sess.parts if part)
        formatted = self._format_ansi_message(rendered)
        if sess.posted is None:
            sess.posted = await message.channel.send(formatted)
            sess.last_flush_at = time.monotonic()
            return
        if getattr(sess.posted, "content", None) == formatted:
            return
        edit = getattr(sess.posted, "edit", None)
        if not callable(edit):
            sess.posted = await message.channel.send(formatted)
            sess.last_flush_at = time.monotonic()
            return
        try:
            await edit(content=formatted)
            sess.last_flush_at = time.monotonic()
        except Exception:
            # Rate-limits / transient Discord errors: keep the existing
            # message and let the next tick retry. Do not post a second dump.
            return

    async def _begin_shell_progress(self, message: Message, text: str):
        sess = _get_shell_progress_turn(self.bot, message)
        async with sess.lock:
            slot = len(sess.parts)
            sess.parts.append(text)
            await self._flush_shell_progress_unlocked(message, sess)
            return sess, slot

    async def _finish_shell_progress(
        self, message: Message, sess: _ShellProgressTurn, slot: int, text: str
    ) -> None:
        async with sess.lock:
            while len(sess.parts) <= slot:
                sess.parts.append("")
            sess.parts[slot] = text
            await self._flush_shell_progress_unlocked(message, sess)

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

        # Indirect-prompt-injection defense: if the current turn is tainted
        # (the model just read content from a URL / web search that may carry
        # prompt-injection payloads), refuse. A fresh user message starts a
        # clean turn. There is no confirmation override.
        if _taint_gate_blocks(self, message, kwargs):
            return (
                "Error: shell refused: this turn read content from a fetched "
                "URL/web search that may carry prompt-injection payloads. "
                "Send a new message without fetched content to run shell."
            )

        tenant = shell_tenant(message)
        if tenant is None:
            return "Error: shell needs an authenticated Discord user and request scope"

        sess = None
        slot = None
        # In DMs, never spam shell progress status messages
        # Real discord.Message always exposes ``guild`` (None for a DM).
        # Lightweight callers/tests may omit it; treat those as channel-like
        # so the durable progress message remains observable.
        is_dm = hasattr(message, "guild") and not getattr(message, "guild", None)
        progress_enabled = not is_dm
        progress_checker = getattr(self.bot, "_progress_enabled", None)
        if progress_enabled and callable(progress_checker):
            guild = getattr(message, "guild", None)
            if guild is not None:
                progress_enabled = bool(
                    progress_checker(str(getattr(guild, "id", "") or ""))
                )
        if progress_enabled:
            try:
                # Keep one durable, user-visible liveness message for this shell
                # call when the shared per-server progress setting permits it.
                # The normal tool-progress message is owned by bot.py and may
                # be stopped as soon as the tool posts; shell commands need
                # their own turn-scoped message when progress is enabled.
                sess, slot = await self._begin_shell_progress(message, "working on it…")
            except Exception:
                # A progress post must never prevent the command itself from
                # running in a restricted channel or a non-Discord caller.
                sess, slot = None, None
        self._signal_streaming(message)

        async def _on_progress(stdout_b, stderr_b, elapsed) -> None:
            if sess is None or slot is None:
                return
            # Keep the channel indicator terse. Captured output belongs in the
            # tool result; mirroring it into Discord can expose secrets and
            # turns a long command into an edit-rate-limit stream.
            del stdout_b, stderr_b, elapsed
            await self._finish_shell_progress(message, sess, slot, "working on it…")

        try:
            stdout, stderr, exit_code = await self._run_shell_command(
                normalized, tenant, on_progress=_on_progress
            )
        except asyncio.TimeoutError:
            if sess is not None and slot is not None:
                with contextlib.suppress(Exception):
                    await self._finish_shell_progress(
                        message,
                        sess,
                        slot,
                        f"Command timed out after {self._timeout_seconds()}s",
                    )
            return f"Error: Command timed out after {self._timeout_seconds()}s"
        except Exception as e:
            logger.warning("shell execution failed (%s)", type(e).__name__)
            if sess is not None and slot is not None:
                with contextlib.suppress(Exception):
                    await self._finish_shell_progress(
                        message,
                        sess,
                        slot,
                        "Shell isolation is unavailable; no host fallback was attempted.",
                    )
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

        max_out = self._max_output()
        if max_out and len(combined) > max_out:
            combined = combined[:max_out] + "\n... (truncated)"

        result = combined if combined else "(command produced no output)"

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
        relative = self._workspace_relative_path(path)
        if tenant is None or relative is None:
            return None, None, "path must identify a file inside /workspace"
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
