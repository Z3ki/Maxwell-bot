"""Tool implementations for the shell plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

class ShellTool(Tool):
    """Execute shell commands in the dedicated Docker sandbox."""
    tool_name = 'shell'
    returns_result = True
    ends_turn = False


    # Shell executes arbitrary code in a container. It's the most dangerous
    # tool we expose, so it gets the taint-check / user-confirmation gate.
    is_destructive = True

    CONTAINER_NAME = "maxwell-shell"
    IMAGE_NAME = "maxwell-shell"
    DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker")
    # Bump when docker-run flags or recycle policy change so an old sandbox
    # is replaced instead of reused.
    _SANDBOX_INIT = "4"

    # Output / command-length caps. Read from env so the operator can tune
    # without a code change. 0 = unlimited (use with care; see below).
    # Defaults are generous: 100k chars of captured output covers any sane
    # `cat /var/log/*` or `find` invocation, and 64k command length is enough
    # for a multi-line ffmpeg pipeline. If you actually need more, raise
    # MAXWELL_SHELL_MAX_OUTPUT / MAXWELL_SHELL_MAX_COMMAND_LENGTH in .env.
    #
    # Why not just remove the caps entirely? Because we still have to fit
    # the response through Discord (2000 char chunks) AND through the LLM
    # context window. A 50 MB stdout will OOM the model long before it
    # OOMs us. 0/unlimited is fine if you've tuned your context budget.
    _MAX_OUTPUT_DEFAULT = 100_000
    _MAX_COMMAND_LENGTH_DEFAULT = 65_536
    # Channel post cap. Captured stdout can be 100k for the model, but posting
    # that as Discord ```ansi``` chunks floods the chat. Visible dump is ~300
    # chars (one short codeblock); the LLM still gets the longer capture.
    _CHANNEL_MAX_CHARS_DEFAULT = 300
    _CHANNEL_MAX_CHUNKS = 1

    # Hard ceiling on shell timeout. The actual timeout is read from env at
    # call time so the operator can raise/lower it, but we never let it
    # exceed this regardless of config. Why a cap? Because the tool runs
    # arbitrary code, and a runaway `cat /dev/zero` or `apt install
    # chromium` can pin a core forever. The cap is high (1 hour) but not
    # gone. If you find yourself wanting to remove it, you probably want
    # a different tool (a job queue, not a chatbot tool call).
    _TIMEOUT_CEILING_SECONDS = 3600

    # Idle recycle. A public bot sharing one sandbox otherwise accumulates
    # packages, daemons, /tmp junk, and bind-mount files. Default 10 minutes
    # unused → docker rm -f and wipe shelldocker/; next shell call starts clean.
    # 0 disables (persistent container, homelab-only).
    _IDLE_SECONDS_DEFAULT = 600
    _last_used_monotonic: float = 0.0
    _idle_reaper_task: asyncio.Task | None = None

    @classmethod
    def _max_output(cls) -> int:
        """Captured stdout+stderr cap. 0 = unlimited."""
        raw = os.environ.get("MAXWELL_SHELL_MAX_OUTPUT", "").strip()
        if not raw:
            return cls._MAX_OUTPUT_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._MAX_OUTPUT_DEFAULT
        return max(0, v)  # 0 means unlimited

    @classmethod
    def _max_command_length(cls) -> int:
        """Max chars in a single shell command. 0 = unlimited."""
        raw = os.environ.get("MAXWELL_SHELL_MAX_COMMAND_LENGTH", "").strip()
        if not raw:
            return cls._MAX_COMMAND_LENGTH_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._MAX_COMMAND_LENGTH_DEFAULT
        return max(0, v)

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
        return max(0, v)

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
        """Seconds unused before the sandbox is destroyed. 0 = never."""
        raw = os.environ.get("MAXWELL_SHELL_IDLE_SECONDS", "").strip()
        if not raw:
            return cls._IDLE_SECONDS_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._IDLE_SECONDS_DEFAULT
        return max(0, min(v, cls._TIMEOUT_CEILING_SECONDS))

    # Serialize container lifecycle + exec so parallel tool batches cannot
    # race docker rm -f / recreate.
    _lifecycle_lock = asyncio.Lock()

    @staticmethod
    def _full_host_access() -> bool:
        """Opt-in host RCE mode. Default is isolated (no /host, no host net)."""
        return os.environ.get("MAXWELL_SHELL_FULL_HOST", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def get_description(self):
        # Surface live limits so the model doesn't have to guess. Pulled at
        # description-build time, which happens per-turn on tool registration.
        max_out = self._max_output()
        max_cmd = self._max_command_length()
        to = self._timeout_seconds()
        max_out_str = "unlimited" if max_out == 0 else f"{max_out:,} chars"
        max_cmd_str = "unlimited" if max_cmd == 0 else f"{max_cmd:,} chars"
        chan = self._channel_max_chars()
        chan_str = "unlimited" if chan == 0 else f"{chan} chars"
        idle = self._idle_seconds()
        if idle:
            persist_note = (
                f"Sandbox and /home/maxwell are wiped after {idle}s idle "
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
            "(comma-separated paths under /home/maxwell)."
        )
        if self._full_host_access():
            return (
                "Run bash -lc in the maxwell-shell container as root (FULL ACCESS: "
                "host net, /host, all capabilities). Params: command (required), "
                "files (optional paths to attach). "
                f"{how} {limits_note}"
            )
        return (
            "Run bash -lc as root with full capabilities in the maxwell-shell "
            "sandbox (workdir /home/maxwell). Params: command (required), files "
            "(optional paths under /home/maxwell to attach to the channel). "
            f"{how} Max 10 MB per file. {limits_note}"
        )

    @classmethod
    async def _run_docker(cls, *args: str, timeout: int = 30):
        return await _run_docker_cmd(*args, timeout=timeout)

    def _should_recycle_running(self) -> bool:
        """True when a running sandbox is too old or its idle age is unknown."""
        idle = self._idle_seconds()
        if idle <= 0:
            return False
        if type(self)._last_used_monotonic <= 0:
            # Process just started (or the reaper already collected). Do not
            # inherit leftover packages/daemons from a previous bot process.
            return True
        return (
            time.monotonic() - type(self)._last_used_monotonic
        ) >= idle

    @classmethod
    async def _wait_container_gone(cls) -> None:
        for _ in range(100):
            (_stdout, _stderr), inspect_code = await cls._run_docker(
                "inspect",
                "--type",
                "container",
                "-f",
                "{{.Id}}",
                cls.CONTAINER_NAME,
                timeout=10,
            )
            if inspect_code != 0:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(
            "sandbox container did not disappear after removal"
        )

    @classmethod
    def _workspace_host_path(cls) -> str:
        """Host path bind-mounted at /home/maxwell. Repo-root shelldocker/."""
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "shelldocker")
        )

    @classmethod
    def _wipe_workspace(cls) -> None:
        """Delete bind-mount contents. Keeps the directory itself."""
        path = os.path.abspath(cls._workspace_host_path())
        if os.path.basename(path) != "shelldocker":
            logger.warning("refusing to wipe shell workspace at %s", path)
            return
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            return
        for name in os.listdir(path):
            child = os.path.join(path, name)
            try:
                if os.path.islink(child) or os.path.isfile(child):
                    os.unlink(child)
                elif os.path.isdir(child):
                    shutil.rmtree(child, ignore_errors=True)
            except Exception as exc:
                logger.warning("shell workspace wipe failed for %s: %s", child, exc)

    @classmethod
    async def _destroy_container_unlocked(cls) -> None:
        """docker rm -f the sandbox, then wipe /home/maxwell. Holds _lifecycle_lock."""
        (_stdout, _stderr), rm_code = await cls._run_docker(
            "rm", "-f", cls.CONTAINER_NAME, timeout=10
        )
        if rm_code != 0:
            (_stdout, _stderr), inspect_code = await cls._run_docker(
                "inspect",
                "--type",
                "container",
                "-f",
                "{{.Id}}",
                cls.CONTAINER_NAME,
                timeout=10,
            )
            if inspect_code == 0:
                raise RuntimeError(
                    "could not remove the existing sandbox container"
                )
        else:
            await cls._wait_container_gone()
        cls._wipe_workspace()

    @classmethod
    def _mark_used(cls) -> None:
        cls._last_used_monotonic = time.monotonic()

    @classmethod
    def _schedule_idle_reaper(cls) -> None:
        if cls._idle_seconds() <= 0:
            return
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
                if idle <= 0 or cls._last_used_monotonic <= 0:
                    return
                remaining = idle - (time.monotonic() - cls._last_used_monotonic)
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 30.0))
                    continue
                async with cls._lifecycle_lock:
                    idle = cls._idle_seconds()
                    if idle <= 0 or cls._last_used_monotonic <= 0:
                        return
                    if time.monotonic() - cls._last_used_monotonic < idle:
                        continue
                    await cls._destroy_container_unlocked()
                    cls._last_used_monotonic = 0.0
                    logger.info(
                        "idle-recycled %s after %ss unused",
                        cls.CONTAINER_NAME,
                        idle,
                    )
                    return
        finally:
            if cls._idle_reaper_task is asyncio.current_task():
                cls._idle_reaper_task = None

    @classmethod
    async def shutdown_sandbox(cls) -> None:
        """Cancel the idle reaper and drop the sandbox (plugin teardown)."""
        task = cls._idle_reaper_task
        cls._idle_reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        async with cls._lifecycle_lock:
            with contextlib.suppress(Exception):
                await cls._destroy_container_unlocked()
            cls._last_used_monotonic = 0.0

    async def _ensure_container(self):
        # Reuse a running container only while it is within the idle window
        # and the access mode still matches. Stopped or stale sandboxes are
        # destroyed, not restarted, so leftover packages/daemons die with them.
        desired_mode = "full" if self._full_host_access() else "isolated"
        try:
            (stdout, _stderr), code = await self._run_docker(
                "inspect",
                "--type",
                "container",
                "-f",
                '{{.State.Running}} {{index .Config.Labels "maxwell.shell.mode"}} '
                '{{index .Config.Labels "maxwell.shell.init"}}',
                self.CONTAINER_NAME,
                timeout=10,
            )
            if code == 0:
                parts = stdout.decode(errors="replace").strip().split(None, 2)
                running = (parts[0] if parts else "").lower() == "true"
                mode = parts[1] if len(parts) > 1 else ""
                init = parts[2] if len(parts) > 2 else ""
                if (
                    running
                    and mode == desired_mode
                    and init == self._SANDBOX_INIT
                    and not self._should_recycle_running()
                ):
                    return
                await self._destroy_container_unlocked()
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

        workspace = self._workspace_host_path()
        os.makedirs(workspace, exist_ok=True)
        shell_host = docker_bind_path(workspace)
        run_args = self._sandbox_run_args(
            full_host=self._full_host_access(), shell_host=shell_host
        )
        (_stdout, stderr), run_code = await self._run_docker(*run_args, timeout=30)
        if run_code != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "docker run failed"
            )

    def _sandbox_run_args(self, *, full_host: bool, shell_host: str) -> list[str]:
        """docker run argv for the persistent sandbox. Root, full capabilities."""
        mode = "full" if full_host else "isolated"
        run_args = [
            "run",
            "-d",
            "--init",
            "--name",
            self.CONTAINER_NAME,
            "--user",
            "0",
            "--label",
            f"maxwell.shell.mode={mode}",
            "--label",
            f"maxwell.shell.init={self._SANDBOX_INIT}",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "--cpus",
            "2.0",
            "--pids-limit",
            "1024",
            "--ulimit",
            "nofile=1024:2048",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=256m",
            "-v",
            f"{shell_host}:/home/maxwell:rw",
        ]
        if full_host:
            # Explicit opt-in: host network + full host FS (documented RCE for admins).
            run_args.extend(["--network", "host", "-v", "/:/host:rw"])
        else:
            # Isolated from the host FS/net, but root with every capability
            # inside the sandbox so apt/chown/bind/raw-sockets just work.
            run_args.extend(["--network", "bridge"])
        run_args.append(self.IMAGE_NAME)
        return run_args

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
        """Return an error reason if the command looks dangerous, otherwise None."""
        if not command:
            return "empty command"
        # 0 = unlimited (operator opts in via MAXWELL_SHELL_MAX_COMMAND_LENGTH=0)
        max_len = self._max_command_length()
        if max_len and len(command) > max_len:
            return f"command too long (max {max_len} chars; set MAXWELL_SHELL_MAX_COMMAND_LENGTH=0 to disable)"
        # Multi-line commands & heredocs are allowed.
        if "\n" in command:
            hint = _unterminated_heredoc_error(command)
            if hint:
                return "heredoc error — " + hint
        non_heredoc = _strip_heredoc_blocks(command)
        if any(ord(c) < 32 and c not in ("\t", "\n", "\r") for c in non_heredoc):
            return "control characters are not allowed in shell commands"
        for pattern in _SHELL_BLOCKED_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return "blocked dangerous shell pattern"
        return None

    _PROGRESS_TICK_SECONDS = 0.8

    async def _run_shell_command(self, command: str, on_progress=None):
        sanitized = self._normalize_command(command)
        validation_error = self._validate_command(sanitized)
        if validation_error:
            raise RuntimeError(validation_error)
        if not sanitized:
            raise RuntimeError("empty command")
        async with self._lifecycle_lock:
            await self._ensure_container()
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
                    "/home/maxwell",
                    "--user",
                    "root",
                    self.CONTAINER_NAME,
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
                        if max_output:
                            remaining = max_output - captured
                            if remaining > 0:
                                kept = chunk[:remaining]
                                buf.extend(kept)
                                captured += len(kept)
                            if len(kept if remaining > 0 else b"") < len(chunk):
                                output_truncated = True
                        else:
                            buf.extend(chunk)
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
                    await self._kill_container_exec(pid_file)
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
                    raise
                except asyncio.CancelledError:
                    # Outer autonomy wait_for or other cancel can hit here; always kill child.
                    await self._kill_container_exec(pid_file)
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
                            await self._kill_container_exec(pid_file)
                            proc.kill()
                            await proc.wait()
                        except Exception as e:
                            # Usually means the process already exited.
                            logger.debug("shell zombie cleanup: %s", e)
                if output_truncated:
                    stderr_buf.extend(b"\n[output truncated at MAXWELL_SHELL_MAX_OUTPUT]")
                return bytes(stdout_buf), bytes(stderr_buf), proc.returncode
            finally:
                type(self)._mark_used()
                type(self)._schedule_idle_reaper()

    async def _kill_container_exec(self, pid_file: str) -> None:
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
                self.CONTAINER_NAME,
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
            preview = normalized[:200] + ("..." if len(normalized) > 200 else "")
            return (
                "Error: shell refused: this turn read content from a fetched "
                "URL/web search that may carry prompt-injection payloads. "
                "Send a new message without fetching that content to run shell.\n"
                f"Command preview: {preview}"
            )

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
                normalized, on_progress=_on_progress
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
            if sess is not None and slot is not None:
                with contextlib.suppress(Exception):
                    await self._finish_shell_progress(
                        message, sess, slot, f"Error: {e}"
                    )
            return f"Error executing command: {e}"

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

    async def _send_container_file(self, message: Message, rel_path: str) -> str | None:
        """Copy a file out of the container, stage it in data/exports/, and
        send it to Discord. Returns filename on success.

        Staging into data/exports/ (which send_file already allowlists) means a
        follow-up `send_file path=.../exports/<name>` can re-attach the same
        artifact without another docker cp — the round-trip is one-shot.
        """
        # Sanitize — no path traversal escapes from /home/maxwell
        clean = rel_path.strip().lstrip("/")
        # The model usually passes a full container path like
        # /home/maxwell/img/foo.png (the system prompt tells it to). lstrip
        # only killed the leading slash, so strip the home/maxwell prefix
        # too — otherwise we re-prepend it and docker cp looks for
        # /home/maxwell/home/maxwell/img/foo.png (which is the bug we're fixing).
        clean = re.sub(r"^home/maxwell/?", "", clean)
        if ".." in clean:
            logger.warning(f"Shell file send blocked — path traversal: {rel_path}")
            return None

        container_path = f"/home/maxwell/{clean}"
        tmp_dir = tempfile.mkdtemp(prefix="maxwell_shell_")
        local_path = os.path.join(tmp_dir, os.path.basename(clean))

        try:
            (_stdout, stderr), code = await self._run_docker(
                "cp", f"{self.CONTAINER_NAME}:{container_path}", local_path, timeout=15
            )
            if code != 0:
                logger.warning(
                    f"docker cp failed for {container_path}: {stderr.decode(errors='replace')}"
                )
                return None

            if not os.path.isfile(local_path):
                logger.warning(f"File not found after docker cp: {local_path}")
                return None

            file_size = os.path.getsize(local_path)
            if file_size > 10 * 1024 * 1024:
                logger.warning(f"Shell file too large to send: {file_size} bytes")
                return None

            filename = os.path.basename(clean)
            # Step aside for the live progress message before posting
            # the file artifact.
            self._signal_streaming(message)
            await message.channel.send(file=File(local_path, filename=filename))
            logger.info(f"Sent shell file: {filename} ({file_size} bytes)")

            # Stage a copy into the canonical exports dir for later re-attach.
            try:
                exports_dir = _shell_exports_dir()
                os.makedirs(exports_dir, exist_ok=True)
                staged = os.path.join(exports_dir, filename)
                # Avoid clobbering an existing export with the same name.
                if os.path.exists(staged):
                    base, ext = os.path.splitext(filename)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                    staged = os.path.join(exports_dir, f"{base}_{stamp}{ext}")
                shutil.copy2(local_path, staged)
                logger.info(f"Staged shell file to exports: {staged}")
            except Exception as e:
                logger.warning(f"Failed to stage shell file to exports: {e}")

            return filename
        except asyncio.TimeoutError:
            logger.warning(f"docker cp timed out for {container_path}")
            return None
        except Exception as e:
            logger.warning(f"Failed to send shell file {rel_path}: {e}")
            return None
        finally:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)
