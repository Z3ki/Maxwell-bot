"""Chat stays responsive while jobs own execution, cancellation and delivery."""

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import _current_request_provider, _current_inbound_effects
from maxwell_core.providers.models import ProviderResult, ProviderPolicy
from message_pipeline import ReplyQueue
from plugins.maxwell_extras.task_jobs import TaskJobs, ManageTasksTool
from plugins.maxwell_extras.task_job_store import TaskJobStore, active_job
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from privacy_data import delete_data, delete_live_data
from test_premium_task_workers import setup, TASKS, call


def environment(root):
    bot, msg, shell, engine = setup(root)
    msg.channel.send = AsyncMock(return_value=SimpleNamespace(id=900))
    bot._user_preferences = UserPreferenceStore(
        root / "plugins/maxwell_extras/user_preferences.json"
    )
    manager = bot._task_jobs = TaskJobs(
        bot, root / "plugins/maxwell_extras/task_jobs.sqlite3"
    )
    bot.tools["delegate_task"] = engine
    bot.tools["manage_tasks"] = ManageTasksTool(bot)
    bot.config.DATA_DIR = str(root)
    return bot, msg, shell, engine, manager


async def launch(engine, manager, msg):
    receipt = json.loads(await engine.execute(msg, tasks=TASKS))
    return manager.jobs[receipt["id"]]


async def join(job):
    await asyncio.wait_for(asyncio.shield(job.task), 3)


def test_same_channel_chat_and_reply_queue_remain_available_before_worker_finishes(
    tmp_path,
):
    async def run():
        bot, msg, shell, engine, manager = environment(tmp_path)
        entered, release, launched, chatted = (asyncio.Event() for _ in range(4))
        main_answers = []
        worker_rounds = 0

        async def generate(messages, **kwargs):
            nonlocal worker_rounds
            if active_job.get() is None:
                return ProviderResult("A normal answer while your site is building")
            worker_rounds += 1
            entered.set()
            await release.wait()
            return (
                call()
                if worker_rounds == 1
                else ProviderResult("Verified worker result")
            )

        bot._generate_response.side_effect = generate
        bot._request_journal = SimpleNamespace(
            submit=AsyncMock(
                side_effect=AssertionError("worker touched the chat journal")
            )
        )
        queue = ReplyQueue()

        async def turn(message, content):
            if content == "build":
                main_answers.append(
                    json.loads(await engine.execute(message, tasks=TASKS))
                )
                launched.set()
            else:
                main_answers.append(
                    str(
                        await bot._generate_response(
                            [{"role": "user", "content": content}]
                        )
                    )
                )
                chatted.set()

        queue.bind(turn)
        try:
            queue.submit("45", msg, "build", directed=True)
            await asyncio.wait_for(launched.wait(), 1)
            await asyncio.wait_for(entered.wait(), 1)
            job = manager.jobs[main_answers[0]["id"]]
            second = SimpleNamespace(**vars(msg))
            second.id = 2
            queue.submit("45", second, "can we talk?", directed=True)
            await asyncio.wait_for(chatted.wait(), 1)
            assert not job.task.done() and "normal answer" in main_answers[1]
            assert "running" in manager.summary(second)
            # Ending the chat queue does not own or cancel the worker job.
            await queue.close()
            assert not job.task.done()
            release.set()
            await join(job)
            assert job.row["state"] == "completed" and job.row["delivery"] == "sent"
            assert manager.store.get(job.row["id"])["effects"] == 1
            assert shell.execute.await_count == 1
            msg.channel.send.assert_awaited_once()
            assert "Verified worker result" in msg.channel.send.await_args.args[0]
            assert not manager._users and manager._active == 0
            assert job.message is None
        finally:
            release.set()
            await queue.close()
            await manager.close()

    asyncio.run(run())


def test_job_uses_own_provider_and_context_after_chat_provider_closes(
    tmp_path, monkeypatch
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        owned = SimpleNamespace(
            policy=ProviderPolicy(sensitive_credentials=True), close=AsyncMock()
        )
        parent = SimpleNamespace(close=AsyncMock())
        selected = AsyncMock(return_value=(owned, True))
        monkeypatch.setattr(
            "plugins.maxwell_extras.worker_providers.select_worker_provider", selected
        )

        async def generate(*args, **kwargs):
            assert _current_request_provider.get() is owned
            assert _current_inbound_effects.get() is None
            entered.set()
            await release.wait()
            return ProviderResult("done")

        bot._generate_response.side_effect = generate
        token = _current_request_provider.set(parent)
        effects_token = _current_inbound_effects.set(
            {"message_id": "1", "tools_running": 0}
        )
        try:
            job = await launch(engine, manager, msg)
            await parent.close()
            await asyncio.wait_for(entered.wait(), 1)
            assert selected.await_args.args[2] is None
            owned.close.assert_not_awaited()
            assert _current_request_provider.get() is parent
            release.set()
            await join(job)
            owned.close.assert_awaited_once()
        finally:
            _current_request_provider.reset(token)
            _current_inbound_effects.reset(effects_token)
            await manager.close()

    asyncio.run(run())


def test_parent_turn_cancellation_does_not_cancel_accepted_job(tmp_path):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered, release, accepted = (asyncio.Event() for _ in range(3))

        replies = iter([call(), ProviderResult("done")])

        async def generate(*a, **k):
            entered.set()
            await release.wait()
            # A worker must call a tool before its text counts as a result.
            return next(replies)

        bot._generate_response.side_effect = generate

        async def chat():
            await engine.execute(msg, tasks=TASKS)
            accepted.set()
            await asyncio.Event().wait()

        parent = asyncio.create_task(chat())
        try:
            await asyncio.wait_for(accepted.wait(), 1)
            await asyncio.wait_for(entered.wait(), 1)
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await parent
            job = next(iter(manager.jobs.values()))
            assert not job.task.done()
            release.set()
            await join(job)
            assert job.row["state"] == "completed"
            assert bot._generate_response.await_count == 2
        finally:
            release.set()
            parent.cancel()
            await manager.close()

    asyncio.run(run())


def test_status_cancel_are_owned_scoped_and_leave_no_orphan_workers(tmp_path):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered = asyncio.Event()
        stopped = []

        async def generate(*a, **k):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert await manager.inspect("456") == []
            assert await manager.cancel("456", job.row["id"]) == 0
            other = SimpleNamespace(**vars(msg))
            other.channel = SimpleNamespace(id=46)
            assert await manager.inspect("123", message=other) == []
            statuses = json.loads(
                await bot.tools["manage_tasks"].execute(msg, action="status")
            )
            assert statuses[0]["state"] == "running"
            answer = json.loads(
                await bot.tools["manage_tasks"].execute(
                    msg, action="cancel", job_id=job.row["id"]
                )
            )
            assert answer["cancelled"] == 1
            assert stopped and job.task.done() and job.row["state"] == "cancelled"
            assert manager._active == 0 and not manager._users
            msg.channel.send.assert_not_awaited()
        finally:
            await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("is_reply", [True, False])
def test_chat_completion_notifies_source_author_only_when_referenced(tmp_path, is_reply):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        reference = SimpleNamespace(message_id=msg.id)
        if is_reply:
            msg.to_reference = Mock(return_value=reference)
        report = "Verified worker result @everyone @here <@&456>"
        bot._generate_response.side_effect = [call(), ProviderResult(report)]
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            assert job.row["delivery"] == "sent"
            msg.channel.send.assert_awaited_once()
            args, kwargs = msg.channel.send.await_args
            assert report in args[0]
            assert f"<@{msg.author.id}>" not in args[0]
            mentions = kwargs["allowed_mentions"]
            assert mentions.everyone is False
            assert mentions.roles is False
            assert mentions.users is True
            assert mentions.replied_user is is_reply
            assert mentions.to_dict().get("replied_user", False) is is_reply
            if is_reply:
                msg.to_reference.assert_called_once_with(fail_if_not_exists=False)
                assert kwargs["reference"] is reference
                assert kwargs["mention_author"] is True
            else:
                assert "reference" not in kwargs
                assert "mention_author" not in kwargs
        finally:
            await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "private,expired", [(True, False), (True, True), (False, False), (False, True)]
)
def test_app_completion_keeps_visibility_and_never_falls_back_to_public_or_dm(
    tmp_path, private, expired
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        msg.response_visibility = "private" if private else "public"
        msg.interaction = SimpleNamespace(
            channel_id=45,
            is_expired=lambda: expired,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        bot._generate_response.side_effect = [call(), ProviderResult("result")]
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            msg.channel.send.assert_not_awaited()
            if expired:
                assert job.row["delivery"] == "expired"
                msg.interaction.followup.send.assert_not_awaited()
                assert (await manager.inspect("123"))[0]["reports"][0][
                    "report"
                ] == "result"
            else:
                assert job.row["delivery"] == "sent"
                assert (
                    msg.interaction.followup.send.await_args.kwargs["ephemeral"]
                    is private
                )
                kwargs = msg.interaction.followup.send.await_args.kwargs
                assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
                assert "reference" not in kwargs
                assert "mention_author" not in kwargs
            public = SimpleNamespace(**vars(msg))
            public.response_visibility = "public"
            if private:
                assert await manager.inspect("123", message=public) == []
        finally:
            await manager.close()

    asyncio.run(run())


def test_failed_delivery_is_not_retried_and_reports_remain_private_retrievable(
    tmp_path,
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        msg.channel.send.side_effect = OSError("SECRET NETWORK ERROR")
        bot._generate_response.side_effect = [call(), ProviderResult("result")]
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            assert job.row["state"] == "completed" and job.row["delivery"] == "failed"
            await manager.tick()
            msg.channel.send.assert_awaited_once()
            assert "SECRET" not in json.dumps(await manager.inspect("123"))
        finally:
            await manager.close()

    asyncio.run(run())


def test_delete_from_dashboard_store_cancels_live_job_and_prevents_results_resurrection(
    tmp_path,
):
    async def run():
        bot, msg, shell, engine, manager = environment(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(*a, **k):
            entered.set()
            await release.wait()
            return call()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            result = await asyncio.to_thread(
                delete_data, tmp_path, "123", bot._user_preferences
            )
            assert "worker_jobs" in result["deleted"]
            await manager.refresh_user("123")
            release.set()
            await join(job)
            assert not manager.store.get(job.row["id"])
            assert await manager.inspect("123") == [] and manager.summary(msg) == ""
            shell.execute.assert_not_awaited()
            msg.channel.send.assert_not_awaited()
        finally:
            release.set()
            await manager.close()

    asyncio.run(run())


def test_shutdown_marks_interrupted_and_restart_never_replays_or_restores_content(
    tmp_path,
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        msg.content = "PRIVATE REQUEST BODY"
        entered = asyncio.Event()

        async def generate(*a, **k):
            entered.set()
            await asyncio.Event().wait()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        await asyncio.wait_for(entered.wait(), 1)
        await manager.close()
        assert job.task.done()
        fresh = TaskJobs(bot, manager.store.path)
        try:
            rows = await fresh.inspect("123")
            assert rows[0]["state"] == "interrupted" and not rows[0].get("reports")
            assert "PRIVATE REQUEST BODY" not in manager.store.path.read_bytes().decode(
                "latin1"
            )
            assert "PRIVATE REQUEST BODY" not in json.dumps(rows)
            assert (
                json.loads(await fresh.submit(engine, msg, TASKS))["state"]
                == "interrupted"
            )
            assert not fresh.jobs
        finally:
            await fresh.close()

    asyncio.run(run())


def test_metadata_recovery_retention_and_full_deletion_are_content_free(tmp_path):
    root = tmp_path / "plugins/maxwell_extras/task_jobs.sqlite3"
    store = TaskJobStore(root)
    now = datetime.now(timezone.utc).timestamp()
    row = {
        "id": "abcdef123456",
        "user_id": "123",
        "source_id": "1",
        "channel_id": "45",
        "guild_id": "",
        "private": 1,
        "kinds": "shell",
        "created_at": now,
        "updated_at": now,
        "state": "queued",
        "delivery": "pending",
    }
    store.create(row, now)
    store.update(row["id"], state="running", effects=1, delivery="attempting")
    fresh = TaskJobStore(root, recover=True)
    assert (
        fresh.get(row["id"])["state"] == "interrupted"
        and fresh.get(row["id"])["delivery"] == "unknown"
    )
    with fresh.db() as db:
        db.execute("UPDATE jobs SET updated_at=0")
    assert fresh.prune() == 1


def test_duplicate_delivery_and_capacity_do_not_start_duplicate_jobs(tmp_path):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered = asyncio.Event()

        async def generate(*a, **k):
            entered.set()
            await asyncio.Event().wait()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert (
                json.loads(await engine.execute(msg, tasks=TASKS))["id"]
                == job.row["id"]
            )
            second = SimpleNamespace(**vars(msg))
            second.id = 2
            assert "already running" in await engine.execute(second, tasks=TASKS)
            assert len(manager.jobs) == 1 and manager._active == 1
        finally:
            await manager.close()

    asyncio.run(run())


def test_failed_metadata_write_never_launches_worker_or_leaks_capacity(
    tmp_path, monkeypatch
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        monkeypatch.setattr(
            manager.store,
            "create",
            lambda *a: (_ for _ in ()).throw(OSError("storage unavailable")),
        )
        with pytest.raises(OSError):
            await engine.execute(msg, tasks=TASKS)
        assert manager._active == 0 and not manager._users and not manager.jobs
        bot._generate_response.assert_not_awaited()
        await manager.close()

    asyncio.run(run())


def test_cancel_during_metadata_acceptance_leaves_interrupted_not_live_job(
    tmp_path, monkeypatch
):
    import threading

    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = manager.store.create

        def create(*args):
            entered.set()
            assert release.wait(3)
            return original(*args)

        monkeypatch.setattr(manager.store, "create", create)
        pending = asyncio.create_task(engine.execute(msg, tasks=TASKS))
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert manager.store.list("123")[0]["state"] == "interrupted"
        assert not manager.jobs and manager._active == 0
        bot._generate_response.assert_not_awaited()
        await manager.close()

    asyncio.run(run())


def test_private_tasks_slash_and_cancel_buttons_are_user_bound(tmp_path):
    from test_config_connections import Interaction
    from plugins.maxwell_extras.task_jobs_ui import handle_tasks, JobButton

    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered = asyncio.Event()

        async def generate(*a, **k):
            entered.set()
            await asyncio.Event().wait()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            interaction = Interaction(123)
            interaction.data = {"name": "tasks"}
            interaction.followup.send = AsyncMock()
            assert await handle_tasks(bot, interaction)
            payload = interaction.followup.send.await_args.kwargs
            assert payload["ephemeral"] is True and job.row["id"] in payload["content"]
            button = next(
                c
                for c in payload["view"].children
                if isinstance(c, JobButton) and c.action == "cancel"
            )
            await button.callback(Interaction(456))
            assert not job.task.done()
            await button.callback(Interaction(123))
            assert job.task.done() and job.row["state"] == "cancelled"
        finally:
            await manager.close()

    asyncio.run(run())


def test_slash_cancel_stops_workers_in_same_context_but_keeps_other_users_jobs(
    tmp_path,
):
    from plugins.maxwell_extras.command_suite import _handle_cancel
    from test_config_connections import Interaction

    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered = asyncio.Event()

        async def generate(*a, **k):
            entered.set()
            await asyncio.Event().wait()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            interaction = Interaction(123)
            interaction.data = {"name": "cancel"}
            interaction.channel_id = 45
            assert await _handle_cancel(bot, interaction)
            assert job.row["state"] == "cancelled"
        finally:
            await manager.close()

    asyncio.run(run())


def test_user_installed_app_without_server_membership_can_run_non_server_work(tmp_path):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        msg.user_install = True
        msg.guild = SimpleNamespace(id=7)
        msg.interaction = SimpleNamespace(
            channel_id=45,
            is_expired=lambda: False,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        bot.get_guild = lambda gid: None
        bot._generate_response.side_effect = [call(), ProviderResult("done")]
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            assert job.row["state"] == "completed"
            assert job.row["guild_id"] == "7" and job.row["delivery"] == "sent"
        finally:
            await manager.close()

    asyncio.run(run())


def test_revoked_channel_access_stops_worker_before_a_tool_or_result_is_sent(tmp_path):
    async def run():
        bot, msg, shell, engine, manager = environment(tmp_path)
        permitted = True
        member = msg.author
        guild = SimpleNamespace(id=7, get_member=lambda uid: member)
        bot.get_guild = lambda gid: guild
        msg.guild = guild
        msg.channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=permitted
        )

        async def generate(*a, **k):
            nonlocal permitted
            permitted = False
            return call()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            assert job.row["state"] != "completed"
            bot._generate_response.assert_awaited_once()
            shell.execute.assert_not_awaited()
            msg.channel.send.assert_not_awaited()
        finally:
            await manager.close()

    asyncio.run(run())


def test_live_privacy_deletion_cancels_jobs_and_clears_transient_reports(tmp_path):
    async def run():
        bot, msg, shell, engine, manager = environment(tmp_path)
        entered = asyncio.Event()

        async def generate(*a, **k):
            entered.set()
            await asyncio.Event().wait()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            result = await delete_live_data(bot, "123")
            assert "worker_jobs" in result["deleted"]
            assert job.task.done() and not manager.jobs and manager._active == 0
            assert not manager.store.list("123")
            msg.channel.send.assert_not_awaited()
        finally:
            await manager.close()

    asyncio.run(run())


def test_completion_claim_is_durable_idempotent_and_never_reposted_after_restart(
    tmp_path,
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        bot._generate_response.side_effect = [ProviderResult("done")]
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            # Re-entry cannot send the same completion twice.
            # Restore the original delivery context only for this retry simulation.
            from plugins.maxwell_extras.task_jobs import WorkerMessage

            job.message = WorkerMessage(msg)
            job.arguments = {"tasks": TASKS}
            await asyncio.gather(manager.deliver(job), manager.deliver(job))
            msg.channel.send.assert_awaited_once()
            assert manager.store.get(job.row["id"])["delivery"] == "sent"
        finally:
            await manager.close()

    asyncio.run(run())


def test_runtime_drain_counts_detached_jobs_after_chat_turn_ends(tmp_path):
    from runtime_lifecycle import inflight_busy, _inflight_tasks

    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        entered = asyncio.Event()

        async def generate(*a, **k):
            entered.set()
            await asyncio.Event().wait()

        bot._generate_response.side_effect = generate
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert inflight_busy(bot)
            assert job.task in _inflight_tasks(bot)
            await manager.cancel("123", job.row["id"])
            assert not inflight_busy(bot)
        finally:
            await manager.close()

    asyncio.run(run())


def test_fast_completion_waits_for_origin_answer_without_stealing_deferred_reply(
    tmp_path, monkeypatch
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        bot._active_request_messages = {"45": msg}
        bot._generate_response.side_effect = [call(), ProviderResult("fast result")]
        waiting = asyncio.Event()
        original = manager.wait_for_origin

        async def wait(job):
            waiting.set()
            await original(job)

        monkeypatch.setattr(manager, "wait_for_origin", wait)
        job = await launch(engine, manager, msg)
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            assert not job.task.done()
            msg.channel.send.assert_not_awaited()
            bot._active_request_messages.clear()
            await join(job)
            msg.channel.send.assert_awaited_once()
            assert "fast result" in msg.channel.send.await_args.args[0]
        finally:
            await manager.close()

    asyncio.run(run())


def test_report_cache_expiry_does_not_expose_stale_content_without_waiting_for_cleanup(
    tmp_path,
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        bot._generate_response.side_effect = [ProviderResult("transient report secret")]
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            job.finished_at -= 3601
            rows = await manager.inspect("123")
            assert "transient report secret" not in json.dumps(rows)
            assert not rows[0].get("reports") and job.row["id"] not in manager.jobs
        finally:
            await manager.close()

    asyncio.run(run())


def test_conversational_status_does_not_dump_all_completed_transcripts_into_chat_context(
    tmp_path,
):
    async def run():
        bot, msg, _, engine, manager = environment(tmp_path)
        bot._generate_response.side_effect = [
            reply
            for _ in range(6)
            for reply in (call(), ProviderResult("result-" + "x" * 7000))
        ]
        try:
            for index in range(6):
                msg = SimpleNamespace(**vars(msg))
                msg.id = index + 1
                job = await launch(engine, manager, msg)
                await join(job)
            result = json.loads(
                await bot.tools["manage_tasks"].execute(msg, action="status")
            )
            assert len(result) == 5 and sum(bool(r.get("reports")) for r in result) == 1
            assert len(json.dumps(result)) < 12000
            oldest = manager.store.list("123")[-1]
            selected = json.loads(
                await bot.tools["manage_tasks"].execute(
                    msg, action="status", job_id=oldest["id"]
                )
            )
            assert selected[0]["reports"][0]["report"].startswith("result-")
        finally:
            await manager.close()

    asyncio.run(run())


def test_text_only_worker_is_nudged_then_ends_as_no_tools_not_completed(tmp_path):
    # f014bd6: a reply with no tool call ("I'll build it") is not a result.
    async def run():
        bot, msg, shell, engine, manager = environment(tmp_path)
        bot._generate_response.side_effect = [
            ProviderResult("I'll build the complete game") for _ in range(3)
        ]
        job = await launch(engine, manager, msg)
        try:
            await join(job)
            assert bot._generate_response.await_count == 3
            shell.execute.assert_not_awaited()
            nudges = bot._generate_response.await_args.args[0]
            assert sum("You have not called any tools" in str(m) for m in nudges) == 2
            assert job.row["state"] == "incomplete" and job.row["delivery"] == "sent"
            assert job.row["effects"] == 0
            sent = msg.channel.send.await_args.args[0]
            assert "didn't finish" in sent and "no_tools" in sent
            assert "I'll build the complete game" not in sent
            (report,) = (await manager.inspect("123"))[0]["reports"]
            assert report["status"] == "no_tools" and report["completed"] == []
        finally:
            await manager.close()

    asyncio.run(run())
