"""Basic tests for the Pi RPC bridge skeleton (pi_bridge.py).

These test the protocol client structure without requiring a full running Pi + LLM
(offline/no-tools mode or mocks). Full integration tests belong in Docker.

Run: python -m pytest -q tests/test_pi_bridge.py

Keeps parity requirements: no changes to site creator, Caddy paths, or original bot logic.
"""

import asyncio
import pytest

# Explicit import path
from pi_bridge import PiRPCBridge, PiEvent


def test_bridge_imports_and_instantiates():
    """Bridge class can be imported and constructed (no side effects)."""
    b = PiRPCBridge()
    assert b is not None
    assert hasattr(b, "send_prompt")
    assert hasattr(b, "start")
    assert hasattr(b, "stop")
    assert b.cwd  # should default to parent of this file's dir (project root)


def test_build_discord_context_helper():
    b = PiRPCBridge()
    history = [
        {"author": "user1", "content": "hello"},
        {"author": "Maxwell", "content": "hi there"},
    ]
    ctx = b.build_discord_context(history, "what is pi?", meta={"channel": "test"})
    assert "user1: hello" in ctx
    assert "user: what is pi?" in ctx
    assert "meta:" in ctx


def test_bridge_lifecycle_no_proc():
    """Start/stop should be safe even if proc never launches (or fails to find pi)."""
    async def _inner():
        b = PiRPCBridge(pi_cmd=["false"])  # will fail to be a real agent
        # stop before start should be no-op / safe
        await b.stop()

        # starting will raise or set proc to something that dies; we just want no crash in client
        try:
            await b.start()
        except Exception:
            pass  # expected if no real pi or bad cmd
        finally:
            await b.stop()

    asyncio.run(_inner())


def test_pi_event_parsing():
    raw = {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "hello"}}
    ev = PiEvent(raw)
    assert ev.type == "message_update"
    assert ev.raw is raw


def test_send_command_without_proc_raises():
    async def _inner():
        b = PiRPCBridge()
        with pytest.raises(RuntimeError):
            await b.send_prompt("test")

    asyncio.run(_inner())


def test_default_cmd_includes_rpc_mode():
    b = PiRPCBridge()
    assert "--mode" in " ".join(b.pi_cmd)
    assert "rpc" in " ".join(b.pi_cmd)


# Note: Real streaming + tool action roundtrips are tested manually or in Docker
# with actual `pi --mode rpc` + keys + a running bot adapter.
# See pi_bridge.py _smoke_test and PROGRESS_PI_BRAIN_PLAN.md
