"""Pi RPC Bridge for Maxwell Discord Bot (experimental Pi as brain).

Thin Python adapter: spawns `pi --mode rpc` (or talks to Node SDK process),
feeds Discord context as prompts (with multimodal), streams back text deltas
to channels, and maps Pi tool calls / results.

See PROGRESS_PI_BRAIN_PLAN.md for full architecture.
Pi docs (in container): /root/.nvm/.../pi-coding-agent/docs/rpc.md for protocol.

Usage sketch (inside bot or standalone test):
    bridge = PiRPCBridge()
    await bridge.start()
    await bridge.prompt("Hello from Discord", channel_id="123", images=[...])
    # subscribe to events or use callbacks for send_text, execute_tool_action, etc.

Keep this minimal: no LLM loop, no XML parsing here. Pi owns the agent brain.
All reasoning/tool decisions/autonomy/memory via Pi sessions + extensions.

Providers: **Just reads the fucking .env** (exactly like Maxwell).
- Uses python-dotenv to load the project's .env (respects MAXWELL_ENV_FILE).
- Keys like OLLAMA_FALLBACK_API_KEY, AUTONOMY_API_KEY, NVIDIA_API_KEY become available.
- Pi then sees them directly or via the auth.json written by setup-pi-providers.sh.

Docker: optional (general Dockerfile may be used); main is Python subprocess + pi --mode rpc.
Explicit paths used: /root/maxwell/pi_bridge.py , .pi/extensions/...

Run tests: python -m pytest -q ; ruff check . --fix
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# === JUST READ THE FUCKING ENV ===
# Load Maxwell's .env at import time so the bridge + spawned `pi --mode rpc`
# process get the real provider keys (NVIDIA, OpenRouter via fallback, etc).
try:
    from dotenv import load_dotenv
    env_path = os.getenv("MAXWELL_ENV_FILE") or str(Path(__file__).parent / ".env")
    load_dotenv(env_path, override=False)
except Exception:
    # dotenv not present or no .env — we'll just use whatever is in os.environ
    pass

logger = logging.getLogger(__name__)

# Default Pi command. Can override via PI_CMD or env.
# Use --no-session for ephemeral Discord turns, or --session-dir for persistent Pi memory.
DEFAULT_PI_CMD = ["pi", "--mode", "rpc", "--no-session"]

def _get_maxwell_pi_model_args():
    """Map Maxwell's OLLAMA_MODEL (from .env / PM2 env) to Pi --model args.

    Your live setup uses:
      OLLAMA_MODEL=xiaomi/mimo-v2.5
      OLLAMA_BASE_URL=https://openrouter.ai/api/v1

    Pi will use the equivalent so you get the *exact same model* as the current
    production maxwell-bot.
    """
    model = os.getenv("OLLAMA_MODEL") or os.getenv("MAXWELL_MODEL") or ""

    if not model:
        return []

    model_lower = model.lower()

    # Direct mappings for your production setup
    if "mimo-v2.5" in model_lower or "xiaomi/mimo" in model_lower:
        # Your current brain model via OpenRouter
        return ["--model", "openrouter/xiaomi/mimo-v2.5"]

    if "minimax" in model_lower:
        return ["--model", "nvidia/minimaxai/minimax-m3"]

    # If they set a full provider/model already, pass it through
    if "/" in model:
        return ["--model", model]

    # Fallback: let Pi try the name (it does fuzzy/provider matching)
    return ["--model", model]

# Environment for Pi (inherits + extras). Pi reads NVIDIA_API_KEY etc directly.
PI_ENV_OVERRIDES: dict[str, str] = {
    # Example: force a specific model if desired (Pi also accepts --model on cmdline)
    # "PI_DEFAULT_MODEL": "openrouter/anthropic/claude-3.5-sonnet",
}


@dataclass
class PiEvent:
    """Parsed event from Pi stdout (JSONL)."""
    raw: dict[str, Any]
    type: str = field(init=False)

    def __post_init__(self):
        self.type = self.raw.get("type", "unknown")


class PiRPCBridge:
    """Async bridge to Pi agent via RPC mode (subprocess + JSONL stdin/stdout).

    Start with:
        bridge = PiRPCBridge(pi_cmd=..., model=..., provider=...)
        await bridge.start()

    Feed messages:
        await bridge.send_prompt("User said: hi", images_b64=...)

    Listen:
        bridge.on_text_delta = lambda delta, meta: ...
        bridge.on_tool_call = lambda name, args: ...  # or handle via events

    Stop:
        await bridge.stop()
    """

    def __init__(
        self,
        pi_cmd: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        on_text_delta: Optional[Callable[[str, dict], None]] = None,
        on_tool_result: Optional[Callable[[str, Any], None]] = None,
        on_agent_event: Optional[Callable[[PiEvent], None]] = None,
    ):
        self.pi_cmd = pi_cmd or DEFAULT_PI_CMD[:]
        self.cwd = cwd or str(Path(__file__).parent.resolve())
        self.extra_env = {**PI_ENV_OVERRIDES, **(extra_env or {})}
        self.proc: Optional[subprocess.Popen] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stdin_lock = asyncio.Lock()
        self._running = False

        # Callbacks (user of bridge sets these)
        self.on_text_delta = on_text_delta
        self.on_tool_result = on_tool_result
        self.on_agent_event = on_agent_event

        # Simple pending for correlated responses (optional)
        self._pending_responses: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        """Spawn the Pi RPC process. Must be called before sending prompts."""
        if self.proc and self.proc.poll() is None:
            logger.warning("Pi RPC already running")
            return

        env = os.environ.copy()
        env.update(self.extra_env)

        # Make sure the exact same keys from your PM2 maxwell-bot .env are visible to Pi
        # (NVIDIA, the openrouter key behind OLLAMA_FALLBACK_*, etc.)
        if not any(k in env for k in ("NVIDIA_API_KEY", "OLLAMA_FALLBACK_API_KEY", "OPENROUTER_API_KEY")):
            try:
                from dotenv import load_dotenv as _ld
                _ld(str(Path(__file__).parent / ".env"), override=False)
                env = os.environ.copy()
                env.update(self.extra_env)
            except Exception:
                pass
        # Ensure keys from .env are present even if load_dotenv was not called earlier
        # (e.g. when bridge is started from a clean subprocess)
        if not env.get("NVIDIA_API_KEY") and not env.get("OLLAMA_FALLBACK_API_KEY"):
            try:
                from dotenv import load_dotenv as _load_dotenv
                _load_dotenv(str(Path(__file__).parent / ".env"), override=False)
                env = os.environ.copy()
                env.update(self.extra_env)
            except Exception:
                pass

        # Ensure we can find pi (in PATH or nvm). In Docker it's global.
        cmd = self.pi_cmd + _get_maxwell_pi_model_args()
        # Restrict Pi to ONLY be the brain + tool caller.
        # No builtin tools (read, bash, edit, write, ls, grep, find) so it cannot
        # arbitrarily list/edit your files or project.
        # Only our controlled Maxwell extensions provide the tools (discord actions,
        # create_site to specific dir, web, image, yt, etc.).
        cmd.extend(["--no-builtin-tools", "--no-skills", "--no-prompt-templates"])
        # Always load Maxwell extensions for brain use (site creator exact Caddy parity,
        # web/image/yt, discord actions via ACTION: markers that Python executes).
        # Explicit paths.
        for ext in (
            ".pi/extensions/maxwell-tools/index.ts",
            ".pi/extensions/maxwell-brain/index.ts",
        ):
            ext_path = os.path.join(self.cwd, ext) if not os.path.isabs(ext) else ext
            if os.path.exists(ext_path):
                cmd.extend(["--extension", ext_path])
        logger.info(f"Starting Pi RPC bridge: {' '.join(cmd)} (cwd={self.cwd})")
        logger.info("Using Maxwell OLLAMA_MODEL / providers from .env (mimo-v2.5 etc.) for Pi brain")

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line buffered
                cwd=self.cwd,
                env=env,
            )
        except FileNotFoundError as e:
            logger.error(f"Pi binary not found. Ensure 'pi' in PATH or install. Error: {e}")
            raise

        self._running = True

        # Start async reader for stdout (events)
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._read_events_loop())

        # Also log stderr in background (non-blocking)
        loop.create_task(self._drain_stderr())

        logger.info("Pi RPC bridge started. Ready for prompts via send_prompt().")

    async def stop(self) -> None:
        """Gracefully stop the Pi process."""
        self._running = False
        if self.proc:
            try:
                # Send abort if possible
                await self._send_command({"type": "abort"}, wait_response=False)
            except Exception:
                pass
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                await asyncio.sleep(0.2)
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception as e:
                logger.warning(f"Error terminating Pi proc: {e}")
            self.proc = None

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        logger.info("Pi RPC bridge stopped.")

    async def send_prompt(
        self,
        message: str,
        images: Optional[list[dict]] = None,  # list of {"type":"image", "data": b64, "mimeType": "image/png"}
        streaming_behavior: Optional[str] = None,  # "steer" or "followUp" if already running
        request_id: Optional[str] = None,
    ) -> bool:
        """Send a user prompt (and optional images) to Pi.

        Returns True if accepted (per docs response.success).
        Images follow RPC ImageContent format.
        """
        cmd: dict[str, Any] = {
            "type": "prompt",
            "message": message,
        }
        if images:
            cmd["images"] = images
        if streaming_behavior:
            cmd["streamingBehavior"] = streaming_behavior
        if request_id:
            cmd["id"] = request_id

        resp = await self._send_command(cmd)
        success = bool(resp.get("success", False)) if resp else False
        if not success:
            logger.warning(f"Prompt rejected or failed: {resp}")
        return success

    async def steer(self, message: str, images: Optional[list[dict]] = None) -> bool:
        cmd: dict[str, Any] = {"type": "steer", "message": message}
        if images:
            cmd["images"] = images
        resp = await self._send_command(cmd)
        return bool(resp.get("success", False)) if resp else False

    async def follow_up(self, message: str, images: Optional[list[dict]] = None) -> bool:
        cmd: dict[str, Any] = {"type": "follow_up", "message": message}
        if images:
            cmd["images"] = images
        resp = await self._send_command(cmd)
        return bool(resp.get("success", False)) if resp else False

    async def get_state(self) -> dict[str, Any]:
        resp = await self._send_command({"type": "get_state"})
        return resp.get("data", {}) if resp else {}

    async def prompt_and_collect(self, message: str, images: Optional[list[dict]] = None, timeout: float = 120) -> str:
        """Send prompt and collect full assistant text response (convenience for wiring).
        Also streams deltas via the on_text_delta callback (used for live send in bot).
        """
        collected: list[str] = []
        orig = self.on_text_delta

        def _collector(delta: str, meta: dict):
            collected.append(delta)
            if orig:
                try:
                    orig(delta, meta)
                except Exception:
                    pass

        self.on_text_delta = _collector
        try:
            await self.send_prompt(message, images=images)
            # wait for completion (rough heuristic)
            waited = 0.0
            last_len = 0
            while waited < timeout:
                await asyncio.sleep(0.5)
                waited += 0.5
                if len(collected) > last_len:
                    last_len = len(collected)
                    continue
                if waited > 3 and len(collected) == last_len:
                    break
            return "".join(collected).strip()
        finally:
            self.on_text_delta = orig

    async def send_background_prompt(self, message: str, timeout: float = 60) -> str:
        """For autonomy/REM/intel: send as follow_up or prompt without full streaming.
        Uses Pi sessions for state.
        """
        collected: list[str] = []
        orig = self.on_text_delta

        def _collector(delta: str, meta: dict):
            collected.append(delta)
            if orig:
                try:
                    orig(delta, meta)
                except Exception:
                    pass

        self.on_text_delta = _collector
        try:
            # Use follow_up for background to not interrupt
            await self.follow_up(message)
            waited = 0.0
            last_len = 0
            while waited < timeout:
                await asyncio.sleep(0.5)
                waited += 0.5
                if len(collected) > last_len:
                    last_len = len(collected)
                elif waited > 5 and len(collected) == last_len:
                    break
            return "".join(collected).strip()
        finally:
            self.on_text_delta = orig

    async def abort(self) -> None:
        await self._send_command({"type": "abort"}, wait_response=False)

    # --- Internal ---

    async def _send_command(self, cmd: dict[str, Any], wait_response: bool = True) -> Optional[dict]:
        if not self.proc or self.proc.stdin is None or not self._running:
            raise RuntimeError("Pi RPC bridge not started")

        req_id = cmd.get("id")
        if wait_response and req_id:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending_responses[req_id] = fut

        line = json.dumps(cmd) + "\n"
        async with self._stdin_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except Exception as e:
                logger.error(f"Failed writing to Pi stdin: {e}")
                if req_id and req_id in self._pending_responses:
                    self._pending_responses.pop(req_id, None)
                raise

        if wait_response and req_id:
            try:
                return await asyncio.wait_for(self._pending_responses[req_id], timeout=30)
            except asyncio.TimeoutError:
                self._pending_responses.pop(req_id, None)
                logger.warning(f"Timeout waiting response for id={req_id}")
                return None
            finally:
                self._pending_responses.pop(req_id, None)
        return None

    async def _read_events_loop(self) -> None:
        """Read JSONL from Pi stdout, dispatch events and responses."""
        if not self.proc or self.proc.stdout is None:
            return

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, self.proc.stdout)

        buffer = ""
        while self._running:
            try:
                # Read chunks; split on \n per strict JSONL (see rpc.md)
                chunk = await reader.read(4096)
                if not chunk:
                    await asyncio.sleep(0.05)
                    continue
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.debug(f"Non-JSON from Pi (ignored): {line[:200]}... err={e}")
                        continue

                    await self._dispatch_event(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error reading Pi events: {e}")
                await asyncio.sleep(0.1)

    async def _dispatch_event(self, raw: dict[str, Any]) -> None:
        ev = PiEvent(raw)

        # Handle correlated command responses
        if ev.type == "response" and "id" in raw:
            req_id = raw["id"]
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                if not fut.done():
                    fut.set_result(raw)

        # Streaming text deltas (main output path)
        if ev.type == "message_update":
            assistant_ev = raw.get("assistantMessageEvent", {})
            if assistant_ev.get("type") == "text_delta":
                delta = assistant_ev.get("delta", "")
                if self.on_text_delta:
                    try:
                        self.on_text_delta(delta, raw)
                    except Exception as cb_e:
                        logger.warning(f"on_text_delta callback error: {cb_e}")
                else:
                    # Default: echo for debug / standalone use
                    sys.stdout.write(delta)
                    sys.stdout.flush()

        # Tool execution lifecycle (Pi decided to use a tool; result will come as tool_result msg)
        if ev.type in ("tool_execution_start", "tool_execution_end"):
            if self.on_tool_result:
                try:
                    self.on_tool_result(ev.type, raw)
                except Exception as cb_e:
                    logger.warning(f"on_tool_result cb error: {cb_e}")

        # Agent lifecycle
        if ev.type in ("agent_start", "agent_end", "turn_start", "turn_end"):
            if self.on_agent_event:
                try:
                    self.on_agent_event(ev)
                except Exception as cb_e:
                    logger.warning(f"on_agent_event cb error: {cb_e}")

        # Log other notable events at debug for now
        if ev.type not in ("message_update", "response"):
            logger.debug(f"Pi event: {ev.type} {str(raw)[:300]}")

        # Forward everything to generic listener
        if self.on_agent_event:
            try:
                self.on_agent_event(ev)
            except Exception:
                pass

    async def _drain_stderr(self) -> None:
        if not self.proc or self.proc.stderr is None:
            return
        try:
            while self._running and self.proc.poll() is None:
                line = self.proc.stderr.readline()
                if line:
                    logger.debug(f"[pi stderr] {line.rstrip()}")
                else:
                    await asyncio.sleep(0.1)
        except Exception:
            pass

    # Convenience: build a simple Maxwell-style context string + forward images
    def build_discord_context(self, channel_history: list[dict], current_user_msg: str, **meta) -> str:
        """Helper to serialize some history + facts for the prompt sent to Pi.
        In full impl, richer context (memory, recent users, system personality) here.
        Pi's own session + AGENTS.md will carry personality.
        """
        parts = []
        for h in channel_history[-10:]:  # keep recent; Pi compacts
            who = h.get("author", "user")
            content = h.get("content", "")
            parts.append(f"{who}: {content}")
        parts.append(f"user: {current_user_msg}")
        if meta:
            parts.append("meta: " + json.dumps(meta, default=str))
        return "\n".join(parts)


# --- Standalone smoke test (protocol only; no real LLM without keys) ---
async def _smoke_test():
    """Run a minimal protocol smoke: start, get_state, send simple prompt, observe events.
    In practice needs DISCORD_TOKEN etc? No - this is bridge only. Needs LLM keys for Pi.
    Use --offline or mock provider to test framing without full calls.
    """
    logging.basicConfig(level=logging.INFO)
    bridge = PiRPCBridge(
        pi_cmd=["pi", "--mode", "rpc", "--no-session", "--no-tools", "--offline"],
        on_text_delta=lambda d, m: print(f"[DELTA] {d}", end="", flush=True),
        on_agent_event=lambda e: logger.info(f"[EVENT] {e.type}"),
    )
    try:
        await bridge.start()
        state = await bridge.get_state()
        logger.info(f"Initial state: {state}")
        # Sending a prompt in offline/no-tools may just ack; events may be limited.
        await bridge.send_prompt("Protocol smoke test: reply with 'pong' if alive.")
        await asyncio.sleep(3.0)  # give time for any startup events
        await bridge.abort()
    finally:
        await bridge.stop()
        logger.info("Smoke test complete.")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
