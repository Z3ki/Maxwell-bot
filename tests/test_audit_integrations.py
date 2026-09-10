"""Offline regressions for integration boundary and persistence failures."""

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from captcha_solver import (
    CaptchaSolveError,
    HumanCaptchaServer,
    _BaseSolver,
    _build_solve_page,
)
from providers import OllamaProvider
from utils import JsonStateStore, render_discord_context_text
from x_client import PostBudget, XClient, XError, collect_tweets


def test_x_user_timeline_does_not_treat_profile_as_tweet():
    payload = {
        "data": {
            "user": {
                "result": {
                    "__typename": "User",
                    "rest_id": "99",
                    "legacy": {"screen_name": "alice"},
                    "timeline": [
                        {
                            "__typename": "Tweet",
                            "rest_id": "101",
                            "legacy": {"full_text": "hello"},
                        }
                    ],
                }
            }
        }
    }
    assert [(t.id, t.text) for t in collect_tweets(payload)] == [("101", "hello")]


def test_x_explicit_zero_configuration(tmp_path):
    client = XClient({"posts_per_hour": 0, "cache_seconds": 0}, data_dir=tmp_path)
    assert client.budget.per_hour == 0
    assert client.cache_seconds == 0


def test_x_writes_never_fall_through_after_uncertain_failure(tmp_path):
    client = XClient({}, data_dir=tmp_path)
    first = SimpleNamespace(
        can_write=True,
        configured=lambda: True,
        write=AsyncMock(side_effect=XError("response lost after posting")),
    )
    second = SimpleNamespace(
        can_write=True,
        configured=lambda: True,
        write=AsyncMock(return_value={"id": "duplicate"}),
    )
    client.backends = [first, second]
    with pytest.raises(XError, match="response lost"):
        asyncio.run(client._write("post", text="hello"))
    second.write.assert_not_awaited()


@pytest.mark.parametrize(
    "raw", ["{broken", "[]", '{"posts": "bad"}', '{"posts": ["bad"]}']
)
def test_x_post_budget_fails_closed_on_corrupt_history(tmp_path, raw):
    path = tmp_path / "x_post_log.json"
    path.write_text(raw)
    with pytest.raises((XError, ValueError)):
        asyncio.run(PostBudget(tmp_path).reserve())
    assert path.read_text() == raw


@pytest.mark.parametrize("operation", ["patch", "log"])
@pytest.mark.parametrize("raw", ["{broken", "[]", ""])
def test_state_mutations_preserve_corrupt_files(tmp_path, operation, raw):
    store = JsonStateStore(str(tmp_path), state_file="state.json", log_file="log.json")
    path = store.state_file if operation == "patch" else store.log_file
    path.write_text(raw)
    with pytest.raises(ValueError):
        asyncio.run(
            store.patch_state({"new": 1})
            if operation == "patch"
            else store.append_log_entry({"new": 1})
        )
    assert path.read_text() == raw


def test_state_mutations_initialize_missing_files(tmp_path):
    store = JsonStateStore(str(tmp_path), state_file="state.json", log_file="log.json")
    asyncio.run(store.patch_state({"new": 1}))
    asyncio.run(store.append_log_entry({"new": 1}))
    assert json.loads(store.state_file.read_text()) == {"new": 1}
    assert json.loads(store.log_file.read_text()) == {"entries": [{"new": 1}]}


def test_media_annotation_uses_path_not_query_extension():
    url = "https://example.test/photo.png?name=download.jpg#preview"
    message = SimpleNamespace(content=url)
    assert f"[media URL: image {url}]" in render_discord_context_text(message)


def test_provider_tool_fallback_preserves_stream_callbacks(monkeypatch):
    provider = OllamaProvider("http://example.test", "test", 10, 0.5)
    complete = AsyncMock(
        side_effect=[RuntimeError("tools not supported"), {"content": "ok"}]
    )
    monkeypatch.setattr(provider, "generate_chat_completion", complete)
    token_cb, tool_cb = object(), object()
    asyncio.run(
        provider.generate_response(
            [],
            tools=[{}],
            on_token=token_cb,
            on_tool_call_name=tool_cb,
            custom_tool_calls=True,
        )
    )
    kwargs = complete.await_args_list[1].kwargs
    assert kwargs.get("on_token") is token_cb
    assert kwargs.get("on_tool_call_name") is tool_cb
    assert kwargs.get("custom_tool_calls") is True


@pytest.mark.parametrize(
    "body", [["token"], 42, {"token": ["not a string"]}, {"token": "  "}]
)
def test_human_captcha_rejects_invalid_token_bodies(body):
    async def run():
        server = HumanCaptchaServer()
        future = asyncio.get_running_loop().create_future()
        server._challenges["test"] = {"fut": future}
        request = SimpleNamespace(
            match_info={"cid": "test"}, json=AsyncMock(return_value=body)
        )
        response = await server._handle_solve(request)
        assert response.status == 400
        assert not future.done()

    asyncio.run(run())


def test_captcha_inline_script_escapes_html_terminators():
    attack = '</script><script>alert("x")</script>'
    page = _build_solve_page("test", attack, attack, False)
    assert attack not in page
    assert page.count("</script>") == 2


def test_captcha_poll_enforces_timeout_during_request():
    async def run():
        solver = _BaseSolver("unused", timeout=0.02)

        async def stuck():
            await asyncio.sleep(1)
            return {"status": "ready"}

        with pytest.raises(CaptchaSolveError, match="timed out"):
            await asyncio.wait_for(solver._poll(stuck), timeout=0.2)

    asyncio.run(run())


def test_dns_spf_update_preserves_unrelated_txt_records(monkeypatch):
    module = importlib.import_module("email_integration.setup_dns")
    writes = []
    records = [
        {
            "id": "verification",
            "name": "z3ki.dev",
            "type": "TXT",
            "content": "google-site-verification=test",
        },
        {
            "id": "spf",
            "name": "z3ki.dev",
            "type": "TXT",
            "content": "v=spf1 include:_spf.example.test ~all",
        },
    ]

    def request(token, method, path, body=None):
        if method == "GET":
            if "dns_records" not in path:
                return {"result": {"name": "z3ki.dev"}}
            return {"result": records}
        writes.append((method, path, body))
        return {"success": True}

    monkeypatch.setattr(module, "_cf_request", request)
    assert module.main(["--token", "unused", "--zone-id", "a" * 32, "--domain", "z3ki.dev"]) == 0
    spf_writes = [
        (path, body)
        for _, path, body in writes
        if body and "v=spf1" in body.get("content", "")
    ]
    assert len(spf_writes) == 1
    assert spf_writes[0][0].endswith("/spf")
    assert (
        spf_writes[0][1]["content"]
        == "v=spf1 include:mailgun.org include:_spf.example.test ~all"
    )


@pytest.fixture
def voice_module(monkeypatch):
    import discord_vc_compat

    monkeypatch.setattr(discord_vc_compat, "ensure_voice_recv_compat", lambda: None)
    monkeypatch.setitem(
        sys.modules, "discord.ext.voice_recv", SimpleNamespace(AudioSink=object)
    )
    spec = importlib.util.spec_from_file_location(
        "_audit_voice_live", Path(__file__).resolve().parents[1] / "voice_live.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_voice_first_frame_is_not_duplicated(voice_module):
    LiveSpeechSink = voice_module.LiveSpeechSink

    async def run():
        sink = LiveSpeechSink(
            loop=asyncio.get_running_loop(),
            on_utterance=AsyncMock(),
            guild_id=1,
            control={},
            self_user_id=2,
        )
        try:
            frame = b"\xe8\x03" * 1920
            sink.write(SimpleNamespace(id=3), SimpleNamespace(pcm=frame))
            assert bytes(sink._states[3].active) == frame
        finally:
            sink.cleanup()

    asyncio.run(run())


def test_voice_safe_int_accepts_nonfinite_controls(voice_module):
    _safe_int = voice_module._safe_int
    assert _safe_int(float("inf"), 500) == 500
    assert _safe_int("-inf", 500) == 500


def test_voice_playback_tail_can_shorten_ignore_window(voice_module, monkeypatch):
    monkeypatch.setattr(voice_module.time, "monotonic", lambda: 100.0)

    async def run():
        sink = voice_module.LiveSpeechSink(
            loop=asyncio.get_running_loop(),
            on_utterance=AsyncMock(),
            guild_id=1,
            control={},
            self_user_id=2,
        )
        try:
            sink.set_ignore_until(190.0)
            sink.set_ignore_until(100.5)
            assert sink._ignore_until == 100.5
        finally:
            sink.cleanup()

    asyncio.run(run())


class _Stream:
    def __init__(self, content="ok", usage=None):
        self.body = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
            ]
        }
        if usage:
            self.body["usage"] = usage

    async def iter_any(self):
        yield ("data: " + json.dumps(self.body) + "\n\ndata: [DONE]\n\n").encode()


class _Response:
    status = 200

    def __init__(self, content="ok", usage=None, on_exit=None):
        self.content = _Stream(content, usage)
        self.on_exit = on_exit

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.on_exit:
            await self.on_exit()


class _Session:
    closed = False

    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []

    def post(self, url, *, json, **kwargs):
        self.payloads.append(json)
        return next(self.responses)


def test_provider_preserves_existing_multimodal_parts():
    provider = OllamaProvider("http://example.test", "test", 10, 0.5)
    provider.available = True
    provider._session = session = _Session([_Response()])
    original_parts = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "https://example.test/old.png"}},
    ]
    asyncio.run(
        provider.generate_response(
            [{"role": "user", "content": original_parts}],
            media=[{"b64": "abc", "mime_type": "image/png"}],
        )
    )
    parts = session.payloads[0]["messages"][0]["content"]
    assert parts[:2] == original_parts
    assert len(parts) == 3


def test_provider_embedded_media_routes_to_vision():
    provider = OllamaProvider(
        "http://example.test", "text", 10, 0.5, vision_model="vision"
    )
    provider.available = True
    provider._session = session = _Session([_Response()])
    asyncio.run(
        provider.generate_response(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/image.png"},
                        },
                    ],
                }
            ]
        )
    )
    assert session.payloads[0]["model"] == "vision"


def test_provider_usage_is_not_swapped_during_response_cleanup():
    async def run():
        first_cleaning, second_done = asyncio.Event(), asyncio.Event()

        async def first_exit():
            first_cleaning.set()
            await second_done.wait()

        provider = OllamaProvider("http://example.test", "test", 10, 0.5)
        provider.available = True
        provider._session = _Session(
            [
                _Response(
                    "first", {"prompt_tokens": 11, "completion_tokens": 1}, first_exit
                ),
                _Response("second", {"prompt_tokens": 22, "completion_tokens": 2}),
            ]
        )
        first = asyncio.create_task(provider.generate_response([]))
        await first_cleaning.wait()
        second = await provider.generate_response([])
        second_done.set()
        first = await first
        assert first.usage["prompt_tokens"] == 11
        assert second.usage["prompt_tokens"] == 22

    asyncio.run(run())


def test_repetition_guards_preserve_unclosed_code_fences():
    from response_guard import break_echo_loop, scrub_repetitions

    text = "```python\n" + 'print("same same same")\n' * 20
    assert scrub_repetitions(text) == text
    assert break_echo_loop(text) == text


def test_state_updates_from_separate_store_instances_do_not_lose_keys(tmp_path):
    async def run():
        stores = [
            JsonStateStore(str(tmp_path), state_file="state.json", log_file="log.json")
            for _ in range(12)
        ]
        await asyncio.gather(
            *(store.patch_state({str(i): i}) for i, store in enumerate(stores))
        )
        assert await stores[0].load_state() == {str(i): i for i in range(12)}

    asyncio.run(run())


@pytest.mark.parametrize(
    "error",
    ["unknown field stream_options", "invalid temperature: only 0.6 is allowed"],
)
def test_provider_parameter_repair_retries_same_endpoint(error):
    class ErrorResponse(_Response):
        status = 400

        async def text(self):
            return error

    provider = OllamaProvider(
        "http://primary.test",
        "primary",
        10,
        0.5,
        fallback_base_url="http://fallback.test",
        fallback_model="fallback",
    )
    provider.available = True
    provider._session = session = _Session([ErrorResponse(), _Response()])
    asyncio.run(provider.generate_response([], fast_fallback=True))
    assert [p["model"] for p in session.payloads] == ["primary", "primary"]


def test_captcha_failed_start_does_not_mark_server_running(monkeypatch):
    from captcha_solver import web

    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
    site = SimpleNamespace(start=AsyncMock(side_effect=OSError("port occupied")))
    monkeypatch.setattr(web, "AppRunner", lambda *a, **k: runner)
    monkeypatch.setattr(web, "TCPSite", lambda *a, **k: site)
    server = HumanCaptchaServer()
    with pytest.raises(OSError, match="port occupied"):
        asyncio.run(server.start())
    assert not server.running
    runner.cleanup.assert_awaited_once()
