"""Background sub-agent jobs (jobs.py): caps, budgets, ack-then-deliver.

Style follows the repo: sync test functions, asyncio.run() for async paths,
fake Discord objects instead of a connection.
"""

import asyncio
import json

import pytest

from jobs import (
    BackgroundJobManager,
    SpawnBackgroundTool,
    resolve_job_budgets,
    run_background_job,
)


class FakeConfig:
    OLLAMA_MAX_TOKENS = 16384


class FakeAuthor:
    def __init__(self, uid="111"):
        self.id = uid


class FakeThread:
    def __init__(self):
        self.id = "thread-1"
        self.sent = []
        self.jump_url = "http://thread.local/t1"

    async def send(self, text):
        self.sent.append(text)
        return None


class FakeChannel:
    def __init__(self, cid="222"):
        self.id = cid
        self.sent = []
        self.thread = FakeThread()

    async def send(self, text):
        self.sent.append(text)
        return None


class FakeGuild:
    def __init__(self, gid="333"):
        self.id = gid


class FakeMessage:
    def __init__(self, channel=None, content=",bg a portfolio site"):
        self.channel = channel or FakeChannel()
        self.content = content
        self.author = FakeAuthor()
        self.guild = FakeGuild()
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        self.channel.sent.append(text)
        return None

    async def create_thread(self, name=None, auto_archive_duration=None):
        return self.channel.thread


class StubBot:
    def __init__(self, manager):
        self.bg_jobs = manager
        self._control = {}
        self.config = FakeConfig()


# budgets


def test_budgets_default_to_extended_headroom():
    budgets = resolve_job_budgets({}, FakeConfig())
    assert budgets["max_tokens"] == 32768  # max(16384*2, 32768)
    assert budgets["timeout_seconds"] == 7200
    assert budgets["max_iters"] == 100


def test_budgets_clamp_to_hard_caps():
    budgets = resolve_job_budgets(
        {"bg_max_tokens": 999999, "bg_timeout_seconds": 99999, "bg_max_iters": 9999},
        FakeConfig(),
    )
    assert budgets == {"max_tokens": 131072, "timeout_seconds": 14400, "max_iters": 200}


def test_budgets_zero_means_default(monkeypatch):
    monkeypatch.delenv("BG_MAX_TOKENS", raising=False)
    budgets = resolve_job_budgets({"bg_max_tokens": 0}, FakeConfig())
    assert budgets["max_tokens"] == 32768


def test_budgets_env_override(monkeypatch):
    monkeypatch.setenv("BG_MAX_ITERS", "42")
    budgets = resolve_job_budgets({}, FakeConfig())
    assert budgets["max_iters"] == 42


# manager


def test_manager_caps_per_user_then_global(tmp_path):
    manager = BackgroundJobManager(
        data_path=str(tmp_path / "jobs.json"), max_jobs=2, max_per_user=1
    )
    manager.create(guild_id="g", channel_id="c", user_id="u1", goal="one")
    with pytest.raises(RuntimeError, match="ALREADY_RUNNING"):
        manager.create(guild_id="g", channel_id="c", user_id="u1", goal="two")
    manager.create(guild_id="g", channel_id="c", user_id="u2", goal="three")
    with pytest.raises(RuntimeError, match="ALL_BUSY"):
        manager.create(guild_id="g", channel_id="c", user_id="u3", goal="four")


def test_manager_cancel_owner_vs_stranger(tmp_path):
    manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
    job = manager.create(guild_id="g", channel_id="c", user_id="owner", goal="x")
    ok, _ = manager.cancel(job.id, requester_id="stranger", is_admin=False)
    assert ok is False
    ok, _ = manager.cancel(job.id, requester_id="", is_admin=False)
    assert ok is False
    ok, msg = manager.cancel(job.id, requester_id="owner", is_admin=False)
    assert ok is True and job.id in msg
    assert manager.get(job.id).status == "cancelled"


def test_manager_persistence_round_trip(tmp_path):
    path = str(tmp_path / "jobs.json")
    manager = BackgroundJobManager(data_path=path)
    job = manager.create(guild_id="g", channel_id="c", user_id="u", goal="remember me")
    manager.mark(job.id, status="done", result="did it")
    raw = json.load(open(path, encoding="utf-8"))
    assert raw["jobs"][job.id]["status"] == "done"
    again = BackgroundJobManager(data_path=path)
    assert again.get(job.id).result == "did it"


def test_manager_restart_cancels_inflight(tmp_path):
    path = str(tmp_path / "jobs.json")
    manager = BackgroundJobManager(data_path=path)
    job = manager.create(guild_id="g", channel_id="c", user_id="u", goal="x")
    manager.mark(job.id, status="running")
    manager._save()
    again = BackgroundJobManager(data_path=path)
    assert again.get(job.id).status == "cancelled"


# spawn tool


def test_spawn_tool_acks_and_tracks_job(tmp_path):
    async def scenario():
        manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
        tool = SpawnBackgroundTool(StubBot(manager))
        message = FakeMessage()
        launched = {}

        async def fake_runner(bot, jid):
            launched["jid"] = jid

        import jobs as jobs_mod

        real = jobs_mod.run_background_job
        jobs_mod.run_background_job = fake_runner
        try:
            result = await tool.execute(message, goal="a portfolio site")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            jobs_mod.run_background_job = real
        assert "Background job `" in result
        assert manager.active_count() == 1
        assert launched["jid"] is not None
        return result

    result = asyncio.run(scenario())
    assert "send_message" in result  # ack instruction for the live turn


def test_spawn_tool_refuses_recursion(tmp_path):
    async def scenario():
        manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
        tool = SpawnBackgroundTool(StubBot(manager))
        message = FakeMessage()
        message._bg_job = True
        return await tool.execute(message, goal="nested")

    assert "ALREADY INSIDE" in asyncio.run(scenario())


def test_spawn_tool_second_spawn_tells_model_to_ack(tmp_path):
    async def scenario():
        manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
        tool = SpawnBackgroundTool(StubBot(manager))
        await tool.execute(FakeMessage(), goal="first")
        return await tool.execute(FakeMessage(), goal="second")

    result = asyncio.run(scenario())
    assert "ALREADY RUNNING" in result
    assert "send_message" in result


# runner (ack-then-deliver with stubbed LLM seams)


class RunnerStubBot(StubBot):
    def __init__(self, manager):
        super().__init__(manager)
        self.slot_priority = None
        self.generated_with = {}
        self.generated_calls = []

    def _message_tool_platform(self, message):
        return "discord"

    def _tool_system_prompt(self, platform, message=None, content=None):
        return ""

    def _build_openai_tools(self, platform, message=None, content=None):
        return []

    def _select_tool_protocol(self, openai_tools):
        return False, []

    async def _acquire_ai_slot(self, timeout, *, priority="background", key=""):
        self.slot_priority = priority

    async def _release_ai_slot(self):
        return None

    async def _generate_response(self, messages, **kwargs):
        self.generated_with = dict(kwargs)
        self.generated_calls.append(dict(kwargs))
        return "built it: http://example.local/site"

    def _native_calls_from(self, response):
        return []

    def _recover_text_tool_calls(self, response):
        return [], response

    async def _dispatch_tool_calls(self, message, response, **kwargs):
        return str(response), []


def test_runner_delivers_short_reply_no_ping(tmp_path):
    async def scenario():
        manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
        bot = RunnerStubBot(manager)
        message = FakeMessage()
        job = manager.create(
            guild_id="g", channel_id="222", user_id="111", goal="a portfolio site"
        )
        manager.attach_runtime(job.id, message=message, channel=message.channel)
        await run_background_job(bot, job.id)
        return manager.get(job.id), message, bot

    job, message, bot = asyncio.run(scenario())
    channel = message.channel
    assert job is not None
    assert job.status == "done"
    assert bot.slot_priority == "background"  # user turns outrank it
    worker_call = bot.generated_calls[0] if bot.generated_calls else {}
    assert worker_call.get("disable_reasoning") is False  # full thinking
    assert worker_call.get("max_tokens", 0) >= 32768  # extended output
    # ONE LLM-written reply to the ORIGINAL message: ping on, single URL.
    assert len(message.replies) == 1
    text, kwargs = message.replies[0]
    assert kwargs.get("mention_author") is True
    assert "http://example.local/site" in text
    assert text.count("http") == 1  # single link, never a list
    assert len(channel.sent) == 1
    # thread posts never ping either
    assert all("<@" not in t for t in message.channel.thread.sent)


def test_runner_marks_error_and_notifies(tmp_path):
    class BrokenBot(RunnerStubBot):
        async def _generate_response(self, messages, **kwargs):
            raise RuntimeError("provider down")

    async def scenario():
        manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
        bot = BrokenBot(manager)
        message = FakeMessage()
        job = manager.create(guild_id="g", channel_id="222", user_id="111", goal="x")
        manager.attach_runtime(job.id, message=message, channel=message.channel)
        await run_background_job(bot, job.id)
        return manager.get(job.id), message.channel

    job, channel = asyncio.run(scenario())
    assert job.status == "error"
    assert any("failed" in text for text in channel.sent)


def test_manager_list_text_guild_filtering(tmp_path):
    manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
    j1 = manager.create(guild_id="g1", channel_id="c1", user_id="u1", goal="goal 1")
    j2 = manager.create(guild_id="g2", channel_id="c2", user_id="u2", goal="goal 2")

    lines_g1 = manager.list_text(guild_id="g1")
    assert j1.id in lines_g1
    assert j2.id not in lines_g1

    lines_all = manager.list_text()
    assert j1.id in lines_all
    assert j2.id in lines_all


def test_runner_delivery_is_single_short_reply(tmp_path):
    class LongOutputBot(RunnerStubBot):
        async def _generate_response(self, messages, **kwargs):
            return "A" * 3500 + "\nhttp://example.local/big"

    async def scenario():
        manager = BackgroundJobManager(data_path=str(tmp_path / "jobs.json"))
        bot = LongOutputBot(manager)
        message = FakeMessage()
        job = manager.create(guild_id="g", channel_id="222", user_id="111", goal="long")
        manager.attach_runtime(job.id, message=message, channel=message.channel)
        await run_background_job(bot, job.id)
        return message

    message = asyncio.run(scenario())
    assert len(message.channel.sent) == 1  # never a multi-message wall
    assert len(message.channel.sent[0]) <= 1900



def test_looks_like_progress_markers():
    from jobs import _looks_like_progress

    assert _looks_like_progress(["Wrote index.html → https://x/"])
    assert _looks_like_progress(["Patched app.py and restarted → https://x/"])
    assert _looks_like_progress(["Site created: https://x/"])
    assert _looks_like_progress(["Backend server live: https://x/"])
    assert not _looks_like_progress(["Total lines: 491 Part 1 len: 12380"])
    assert not _looks_like_progress(["Length of index.html: 23706"])
    assert not _looks_like_progress([])


def _tool(name):
    return {"function": {"name": name}}


def test_worker_catalog_hides_spawner_and_channel_post():
    from jobs import _worker_tools

    tools = [_tool("spawn_background"), _tool("send_message"), _tool("shell"), _tool("site_test")]
    kept = {(t["function"]["name"]) for t in _worker_tools(tools)}
    assert kept == {"shell", "site_test"}


def test_delivery_line_vague_final_falls_back_to_thread():
    from jobs import _delivery_line

    assert _delivery_line("I'm all done!", "abc123") == "job `abc123` done — details in the thread."
    assert _delivery_line("done", "abc123") == "job `abc123` done — details in the thread."
    good = _delivery_line("Built BODYCAM // ZERO HOUR: https://maxwell.z3ki.dev/bot/bodycam-zero-hour/ — hyper-realistic bodycam", "abc123")
    assert "https://maxwell.z3ki.dev/bot/bodycam-zero-hour/" in good
    assert good.count("http") == 1


def test_resolve_job_model_precedence(monkeypatch):
    from jobs import BG_MODEL_DEFAULT, resolve_job_model

    monkeypatch.delenv("BG_MODEL", raising=False)
    assert resolve_job_model({}) == BG_MODEL_DEFAULT
    assert resolve_job_model({"bg_model": "  "}) == BG_MODEL_DEFAULT
    monkeypatch.setenv("BG_MODEL", "gemini-3.8-flash-medium")
    assert resolve_job_model({}) == "gemini-3.8-flash-medium"
    assert resolve_job_model({"bg_model": "custom-model"}) == "custom-model"
