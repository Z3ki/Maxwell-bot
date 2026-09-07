"""TTFT / TPS helpers and the ,debug command."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from bot import MaxwellBot
from bot_tools import DebugTool, collect_debug_stats
from providers import compute_llm_timing, format_timing_debug


def test_compute_streaming_ttft_and_tps():
    rec = compute_llm_timing(
        request_start=100.0,
        first_token_s=100.4,
        ended_at=102.4,
        headers_ms=50.0,
        usage={"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1000},
        endpoint="primary",
        model="test-model",
        stream=True,
        content_chars=40,
        tool_calls=1,
    )
    assert rec["ttft_ms"] == 400.0
    assert rec["total_ms"] == 2400.0
    assert rec["gen_ms"] == 2000.0
    assert rec["tps"] == 100.0
    assert rec["prompt_tokens"] == 800
    assert rec["completion_tokens"] == 200
    assert rec["endpoint"] == "primary"


def test_compute_nonstream_uses_total_window():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=None,
        ended_at=2.0,
        usage={"prompt_tokens": 10, "completion_tokens": 40, "total_tokens": 50},
        stream=False,
    )
    assert rec["ttft_ms"] == 2000.0
    assert rec["tps"] == 20.0


def test_compute_streaming_tps_uses_last_token_not_stream_end():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=0.4,
        last_token_s=1.4,
        ended_at=2.0,
        usage={"prompt_tokens": 10, "completion_tokens": 100, "total_tokens": 110},
        stream=True,
    )
    assert rec["ttft_ms"] == 400.0
    assert rec["total_ms"] == 2000.0
    assert rec["gen_ms"] == 1000.0
    assert rec["tps"] == 100.0
    assert rec["tokens_estimated"] is False


def test_compute_estimates_tokens_when_usage_missing():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=0.2,
        last_token_s=1.2,
        ended_at=1.3,
        usage={},
        stream=True,
        content="abcd" * 50,
    )
    assert rec["tokens_estimated"] is True
    assert rec["completion_tokens"] == 50
    assert rec["tps"] == 50.0


def test_compute_accepts_input_output_token_aliases():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=0.1,
        last_token_s=1.1,
        ended_at=1.2,
        usage={"input_tokens": 12, "output_tokens": 40},
        stream=True,
    )
    assert rec["prompt_tokens"] == 12
    assert rec["completion_tokens"] == 40
    assert rec["tps"] == 40.0
    assert rec["tokens_estimated"] is False


def test_streaming_tps_uses_e2e_when_decode_is_a_single_burst():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=1.0,
        last_token_s=1.0,
        ended_at=1.01,
        usage={"completion_tokens": 80},
        stream=True,
    )
    assert rec["ttft_ms"] == 1000.0
    assert rec["gen_ms"] == 0.0
    assert rec["total_ms"] == 1010.0
    assert rec["tps"] == 79.2


def test_tiny_decode_span_does_not_report_thousands_of_tps():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=5.645,
        last_token_s=5.670,
        ended_at=5.6706,
        usage={"completion_tokens": 252},
        stream=True,
    )
    assert rec["gen_ms"] == 25.0
    assert rec["tps"] == round(252 / 5.6706, 1)
    assert rec["tps"] < 200.0


def test_format_timing_debug_empty():
    text = format_timing_debug([], daily={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    assert "no calls recorded" in text
    assert "today 1 in / 2 out" in text


def test_format_timing_debug_last_call():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=0.2,
        ended_at=1.2,
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        endpoint="primary",
        model="m",
        stream=True,
        headers_ms=80.0,
    )
    text = format_timing_debug([rec])
    assert "last call" in text
    assert "ttft 200ms" in text
    assert "headers 80ms" in text
    assert "tps 50.0" in text
    assert "primary" in text


def test_format_timing_debug_weighted_tps():
    short = compute_llm_timing(
        request_start=0.0,
        first_token_s=1.0,
        last_token_s=2.0,
        ended_at=2.1,
        usage={"completion_tokens": 10},
        stream=True,
        endpoint="primary",
    )
    long = compute_llm_timing(
        request_start=0.0,
        first_token_s=1.0,
        last_token_s=1.5,
        ended_at=1.6,
        usage={"completion_tokens": 90},
        stream=True,
        endpoint="primary",
    )
    text = format_timing_debug([short, long])
    assert "avg tps 95.0" in text
    assert "weighted 66.7" in text


def test_collect_debug_stats_from_provider():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=0.1,
        ended_at=1.1,
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        stream=True,
        endpoint="fallback",
        model="x",
    )
    bot = SimpleNamespace(
        ai_provider=SimpleNamespace(_timing_history=[rec], _last_timing=rec),
        _token_tracker=SimpleNamespace(
            summary=lambda: {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            }
        ),
        _reply_queue=SimpleNamespace(depth=lambda _cid: 0),
        _active_requests={},
    )
    text = collect_debug_stats(bot, "99")
    assert "fallback" in text
    assert "tps" in text
    assert "queue depth 0" in text


def test_debug_tool_returns_stats():
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=0.05,
        ended_at=0.55,
        usage={"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
        stream=True,
        endpoint="primary",
        model="m",
    )
    bot = SimpleNamespace(
        ai_provider=SimpleNamespace(_timing_history=[rec], _last_timing=rec),
        _token_tracker=None,
        _reply_queue=None,
        _active_requests={},
    )
    msg = SimpleNamespace(channel=SimpleNamespace(id=7))
    out = asyncio.run(DebugTool(bot).execute(msg))
    assert "ttft" in out
    assert "tps" in out


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.id = 1

    async def send(self, content):
        self.sent.append(content)


class FakeAuthor:
    id = 42
    display_name = "admin"
    bot = False


class FakeMessage:
    def __init__(self, content):
        self.id = 123
        self.content = content
        self.channel = FakeChannel()
        self.author = FakeAuthor()
        self.guild = None
        self.mentions = []
        self.reference = None


def test_debug_command_admin_gating_and_output():
    bot = cast(Any, MaxwellBot.__new__(MaxwellBot))
    bot.command_prefix = ","
    bot._admins = set()
    bot._control = {"disabled_commands": []}
    rec = compute_llm_timing(
        request_start=0.0,
        first_token_s=0.3,
        ended_at=1.3,
        usage={"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
        stream=True,
        endpoint="primary",
        model="demo",
    )
    bot.ai_provider = SimpleNamespace(_timing_history=[rec], _last_timing=rec)
    bot._token_tracker = SimpleNamespace(
        summary=lambda: {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120}
    )
    bot._reply_queue = SimpleNamespace(depth=lambda _cid: 2)
    bot._active_requests = {}

    async def run():
        msg = FakeMessage(",debug")
        await MaxwellBot._handle_command(bot, msg)
        assert msg.channel.sent == ["not authorized"]

        bot._admins = {"42"}
        msg = FakeMessage(",debug")
        await MaxwellBot._handle_command(bot, msg)
        assert len(msg.channel.sent) == 1
        body = msg.channel.sent[0]
        assert body.startswith("```")
        assert "ttft 300ms" in body
        assert "tps 40.0" in body
        assert "queue depth 2" in body

    asyncio.run(run())
