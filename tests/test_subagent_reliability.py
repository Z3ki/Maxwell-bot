"""Reliability hardening for the native sub-agent loop.

Covers two fixes to ``SubAgentTool._agent_loop``:
* a transient provider failure no longer ends a minutes-long run — it retries a
  couple of times with backoff before giving up;
* an empty model reply (no tool call AND no content) is nudged to act rather
  than prematurely ending the run with "stopped without a report".
"""

import asyncio
import json
import types
import time


from bot_tools import SubAgentTool
from config import Config


def _call(name, args, call_id="1"):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def _finish(report="finished ok"):
    return {"role": "assistant", "tool_calls": [_call("finish", {"report": report})]}


class _ScriptedProvider:
    """Yields queued replies (or raises queued exceptions) in order.

    Records every call and the messages it was fed, plus a total call count.
    """

    def __init__(self, queue):
        self.queue = list(queue)
        self.seen = []
        self.calls = 0

    async def generate_chat_completion(self, messages=None, **kwargs):
        self.calls += 1
        self.seen.append([m for m in (messages or [])])
        if not self.queue:
            return {"role": "assistant", "content": "out of script", "tool_calls": []}
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _cfg(tmp_path, monkeypatch, *, retries=2, duds=2):
    monkeypatch.setattr(Config, "SUBAGENT_PROVIDER_RETRIES", retries)
    monkeypatch.setattr(Config, "SUBAGENT_DUD_TOLERANCE", duds)
    # Keep the run well inside its budget so the deadline check never fires.
    return SubAgentTool(types.SimpleNamespace(provider=None))


async def _run(provider, tmp_path, dud_tolerance=2, retries=2, max_steps=50,
               monkeypatch=None):
    monkeypatch.setattr(Config, "SUBAGENT_PROVIDER_RETRIES", retries)
    monkeypatch.setattr(Config, "SUBAGENT_DUD_TOLERANCE", dud_tolerance)
    tool = SubAgentTool(types.SimpleNamespace(provider=provider))
    workspace = tmp_path  # a Path
    deadline = time.monotonic() + 600
    return await tool._agent_loop(
        "build a thing",
        workspace,
        max_steps=max_steps,
        deadline=deadline,
        model="test-model",
        provider=provider,
        bus=None,
        run_id="",
        conv=None,
        message=None,
    )


def test_transient_provider_failure_is_retried(tmp_path, monkeypatch):
    provider = _ScriptedProvider([RuntimeError("transient network blip"), _finish()])
    report = asyncio.run(_run(provider, tmp_path, monkeypatch=monkeypatch))
    assert "finished ok" in report
    # One failed attempt + one successful retry.
    assert provider.calls == 2


def test_retry_exhaustion_fails_the_run(tmp_path, monkeypatch):
    # retries=2 => up to 3 attempts total; all fail => the run fails cleanly.
    provider = _ScriptedProvider(
        [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")]
    )
    report = asyncio.run(_run(provider, tmp_path, retries=2, monkeypatch=monkeypatch))
    assert "model call failed" in report
    assert provider.calls == 3


def test_empty_reply_is_nudged_then_succeeds(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        [
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "assistant", "content": "", "tool_calls": []},
            _finish("done after nudges"),
        ]
    )
    report = asyncio.run(
        _run(provider, tmp_path, dud_tolerance=2, monkeypatch=monkeypatch)
    )
    assert "done after nudges" in report
    # The nudge reminder reached the model on the call that followed a dud.
    reminder_seen = any(
        "Don't stop" in str(m.get("content") or "")
        for call in provider.seen
        for m in call
        if m.get("role") == "user"
    )
    assert reminder_seen


def test_empty_reply_tolerance_ends_run(tmp_path, monkeypatch):
    # dud_tolerance=2 => after a 3rd consecutive empty reply the run gives up.
    provider = _ScriptedProvider(
        [
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "assistant", "content": "", "tool_calls": []},
        ]
    )
    report = asyncio.run(
        _run(provider, tmp_path, dud_tolerance=2, monkeypatch=monkeypatch)
    )
    assert "repeated empty replies" in report
    assert provider.calls == 3


def test_zero_retries_keeps_old_behavior(tmp_path, monkeypatch):
    # Config with retries=0 (disabled) must still fail on the first error.
    provider = _ScriptedProvider([RuntimeError("boom")])
    report = asyncio.run(_run(provider, tmp_path, retries=0, monkeypatch=monkeypatch))
    assert "model call failed" in report
    assert provider.calls == 1
