"""Opt-in storage, consent evidence and truthful deletion across process boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conversation_memory import ConversationMemoryManager
from plugins.maxwell_extras.user_preferences import (
    UserPreferenceStore,
    tracks_message_content,
)
from privacy import (
    ConsentStore,
    STORAGE_VERSION,
    VOICE_VERSION,
    maybe_notice,
    policy_urls,
    voice_policy,
)
from privacy_data import cleanup_data, delete_data, deletion_text


def preferences(root):
    return UserPreferenceStore(root / "plugins/maxwell_extras/user_preferences.json")


def test_new_and_legacy_users_do_not_create_fake_storage_consent(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text(
        json.dumps(
            {
                "users": {
                    "1": {"defaults": {"tracking": "on"}},
                    "2": {"defaults": {"tracking": "off"}},
                }
            }
        )
    )
    store = UserPreferenceStore(path)
    assert all(
        store.get(uid)["defaults"]["tracking"] == "off" for uid in ("1", "2", "3")
    )
    assert store.consents.review("1") == []
    assert tracks_message_content(None, "1") is False
    store.reset_default("2", "tracking")
    assert store.get("2")["defaults"]["tracking"] == "off"
    store.set_default("1", "tracking", "on")
    assert UserPreferenceStore(path).get("1")["defaults"]["tracking"] == "on"
    assert store.consents.granted("1", "history", STORAGE_VERSION, "optional_storage")
    store.reset_default("1", "tracking")
    assert store.get("1")["defaults"]["tracking"] == "off"
    assert store.consents.review("1")[0]["status"] == "withdrawn"


def test_consent_is_scoped_versioned_idempotent_and_durable(tmp_path):
    store = ConsentStore(tmp_path / "consents.db")
    with pytest.raises(ValueError):
        store.record("1", "terms", "1", "agreement")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda _: store.record(
                    "1", "voice:a", "1", "voice_authorization", affirmative=True
                ),
                range(12),
            )
        )
    assert len(store.review("1")) == 1
    assert not store.granted("2", "voice:a", "1", "voice_authorization")
    assert not store.granted("1", "voice:a", "2", "voice_authorization")
    assert not store.granted("1", "history", "1", "optional_storage")
    stamp = store.review("1")[0]["recorded_at"]
    store.record("1", "voice:a", "1", "voice_authorization", affirmative=True)
    assert store.review("1")[0]["recorded_at"] == stamp
    store.withdraw("1")
    restored = ConsentStore(store.path)
    assert restored.review("1")[0]["withdrawn_at"] is not None
    assert not restored.granted("1", "voice:a", "1", "voice_authorization")
    restored.delete("1")
    assert restored.review("1") == []


@pytest.mark.parametrize("variant", ["user", "tool", "bot", "dm", "slash", "edited"])
def test_off_prevents_content_persistence_across_ingestion_rows(tmp_path, variant):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        row = {"author_id": "1", "content": "MUST_NOT_PERSIST", "message_id": "100"}
        if variant == "tool":
            row.update(
                author_id="",
                is_tool=True,
                requested_by="1",
                tool_result="MUST_NOT_PERSIST",
            )
        if variant == "bot":
            row.update(author_id="99", author_is_bot=True, requested_by="1")
        if variant == "slash":
            row["visibility"] = "private"
        if variant == "edited":
            row["edited_at"] = time.time()
        if variant != "dm":
            row["guild_id"] = "10"
        await memory.add_to_channel_memory("20", row)
        assert memory._db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
        await memory.flush()

    asyncio.run(scenario())


def test_delete_cannot_be_undone_by_old_queued_write_or_restart(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        prefs = memory.preferences
        prefs.set_default("1", "tracking", "on")
        old = {
            "author_id": "1",
            "content": "erase me",
            "message_id": "100",
            "privacy_started_at": time.time(),
        }
        await memory.add_to_channel_memory("20", old)
        api = preferences(tmp_path)
        result = await asyncio.to_thread(delete_data, tmp_path, "1", api)
        assert not result["failed"]
        api.set_default("1", "tracking", "on")
        await memory.add_to_channel_memory("20", old)
        assert memory._db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
        await memory.add_to_channel_memory(
            "20", dict(old, message_id="101", privacy_started_at=time.time())
        )
        assert memory._db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 1
        await memory.flush()
        restored = ConversationMemoryManager(str(tmp_path))
        assert restored.preferences.get("1")["defaults"]["tracking"] == "on"
        await restored.flush()

    asyncio.run(scenario())


def test_deletion_partial_failure_is_visible_and_storage_stays_off(
    tmp_path, monkeypatch
):
    import privacy_data

    store = preferences(tmp_path)
    store.set_default("1", "tracking", "on")
    monkeypatch.setattr(
        privacy_data,
        "delete_author_content_file",
        lambda *args: (_ for _ in ()).throw(OSError("DO NOT LOG THIS MESSAGE")),
    )
    result = delete_data(tmp_path, "1", store)
    assert result["failed"] == ["conversation"]
    assert not result["complete"]
    assert "Failed: conversation" in deletion_text(result)
    assert store.get("1")["defaults"]["tracking"] == "off"
    assert "DO NOT LOG" not in deletion_text(result)


def test_corrupt_preferences_and_failed_suppression_abort_safely(tmp_path):
    store = preferences(tmp_path)
    store.path.write_text('{"users":')
    assert not tracks_message_content(store, "1")
    with pytest.raises(ValueError):
        delete_data(tmp_path, "1", store)
    assert store.path.read_text() == '{"users":'


def test_failed_write_does_not_activate_history(tmp_path, monkeypatch):
    import plugins.maxwell_extras.user_preferences as module

    store = preferences(tmp_path)
    monkeypatch.setattr(
        module,
        "_atomic_json_write_sync",
        lambda *args: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        store.set_default("1", "tracking", "on")
    assert not tracks_message_content(store, "1")


def test_dashboard_sync_keeps_opt_out_on_unrelated_update(tmp_path):
    discord = preferences(tmp_path)
    web = preferences(tmp_path)
    discord.set_default("1", "tracking", "off")
    web.update("1", {"language": "Spanish"}, "brief")
    assert not tracks_message_content(discord, "1")
    web.update("1", {"tracking": "on"}, "brief")
    assert tracks_message_content(discord, "1")
    discord.reset_default("1", "tracking")
    assert not tracks_message_content(web, "1")


@pytest.mark.parametrize(
    "base",
    [
        "",
        "http://unsafe.test",
        "https://name:password@operator.test",
        "javascript:alert(1)",
    ],
)
def test_policy_links_never_use_untrusted_or_default_operator_urls(base, monkeypatch):
    for key in ("MAXWELL_TERMS_URL", "MAXWELL_PRIVACY_URL", "MAXWELL_CONTACT_URL"):
        monkeypatch.delenv(key, raising=False)
    assert policy_urls(SimpleNamespace(MAXWELL_PUBLIC_BASE_URL=base)) == {}


def test_selfhosted_policy_override_and_subpath():
    urls = policy_urls(
        SimpleNamespace(
            MAXWELL_PUBLIC_BASE_URL="https://host.test/dev",
            MAXWELL_PRIVACY_URL="https://legal.test/data",
        )
    )
    assert urls["privacy"] == "https://legal.test/data"
    assert urls["terms"] == "https://host.test/dev/terms/"


def test_notice_is_private_once_per_version_without_acceptance(tmp_path, monkeypatch):
    async def scenario():
        store = preferences(tmp_path)
        bot = SimpleNamespace(
            _user_preferences=store,
            config=SimpleNamespace(MAXWELL_PUBLIC_BASE_URL="https://operator.test"),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1), followup=SimpleNamespace(send=AsyncMock())
        )
        await asyncio.gather(*(maybe_notice(bot, interaction) for _ in range(8)))
        assert interaction.followup.send.await_count == 1
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        assert store.consents.review("1") == []
        await maybe_notice(bot, interaction)
        assert interaction.followup.send.await_count == 1
        monkeypatch.setenv("MAXWELL_NOTICE_VERSION", "material-disclosure-2")
        await maybe_notice(bot, interaction)
        assert interaction.followup.send.await_count == 2

    asyncio.run(scenario())


def test_missing_permission_has_no_public_or_dm_fallback(tmp_path):
    async def scenario():
        bot = SimpleNamespace(
            _user_preferences=preferences(tmp_path),
            config=SimpleNamespace(MAXWELL_PUBLIC_BASE_URL="https://operator.test"),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1, send=AsyncMock()),
            channel=SimpleNamespace(send=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(side_effect=PermissionError())),
        )
        await maybe_notice(bot, interaction)
        interaction.user.send.assert_not_awaited()
        interaction.channel.send.assert_not_awaited()
        assert bot._user_preferences.consents.review("1") == []

    asyncio.run(scenario())


def test_cleanup_migrates_legacy_default_on_and_preserves_current_choice(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        store = memory.preferences
        store.set_default("2", "tracking", "on")
        await memory.add_to_channel_memory(
            "20", {"author_id": "2", "content": "keep", "message_id": "2"}
        )
        memory._db.execute(
            "INSERT INTO vectors(id,kind,author_id,content,timestamp,created_at) VALUES ('legacy','message','1','erase','2026-01-01',?)",
            (time.time(),),
        )
        await asyncio.to_thread(cleanup_data, tmp_path, store)
        assert [r[0] for r in memory._db.execute("SELECT content FROM vectors")] == [
            "keep"
        ]
        await memory.flush()

    asyncio.run(scenario())


def test_voice_reference_cannot_be_authorized_by_tool_argument(tmp_path, monkeypatch):
    from plugins.tts_voice.impl import TtsTool

    async def scenario():
        bot = SimpleNamespace(_user_preferences=preferences(tmp_path))
        msg = SimpleNamespace(author=SimpleNamespace(id=1))
        tool = TtsTool(bot)
        denied = await tool.execute(
            msg,
            text="hello",
            reference_audio_url="https://public.test/voice.mp3",
            consent=True,
        )
        assert "authorize this exact" in denied
        assert bot._user_preferences.consents.review("1") == []
        ref = "https://public.test/voice.mp3"
        bot._user_preferences.consents.record(
            "1",
            voice_policy(ref),
            VOICE_VERSION,
            "voice_authorization",
            affirmative=True,
        )
        assert not bot._user_preferences.consents.granted(
            "1", voice_policy(ref + "x"), VOICE_VERSION, "voice_authorization"
        )

    asyncio.run(scenario())


def test_operator_deletion_report_does_not_claim_all_external_copies_removed(tmp_path):
    result = delete_data(tmp_path, "1", preferences(tmp_path))
    assert result["complete"] is False
    assert result["manual_required"]
    assert "backups" in deletion_text(result)
    assert "all data deleted" not in deletion_text(result).lower()


def test_dashboard_deletion_is_authenticated_and_revokes_sessions(
    tmp_path, monkeypatch
):
    from test_dashboard import setup as dashboard_setup

    async def scenario():
        async with dashboard_setup(tmp_path, monkeypatch) as (
            dashboard,
            client,
            headers,
        ):
            dashboard.preferences.set_default("100", "tracking", "on")
            assert (await client.delete("/api/dashboard/privacy")).status == 401
            before = await client.get("/api/dashboard/privacy", headers=headers)
            assert (await before.json())["storage"] == "on"
            response = await client.delete("/api/dashboard/privacy", headers=headers)
            data = await response.json()
            assert response.status == 200
            assert data["complete"] is False and data["manual_required"]
            assert data["failed"] == []
            assert dashboard.preferences.get("100")["defaults"]["tracking"] == "off"
            assert (
                await client.get("/api/dashboard/personal", headers=headers)
            ).status == 401

    asyncio.run(scenario())


def test_dashboard_does_not_return_false_deletion_success(tmp_path, monkeypatch):
    from test_dashboard import setup as dashboard_setup
    import privacy_data

    async def scenario():
        async with dashboard_setup(tmp_path, monkeypatch) as (
            dashboard,
            client,
            headers,
        ):
            monkeypatch.setattr(
                privacy_data,
                "delete_author_content_file",
                lambda *args: (_ for _ in ()).throw(OSError("private deleted text")),
            )
            response = await client.delete("/api/dashboard/privacy", headers=headers)
            data = await response.json()
            assert response.status == 207
            assert data["failed"] == ["conversation"]
            assert data["complete"] is False
            assert dashboard.preferences.get("100")["defaults"]["tracking"] == "off"
            assert "private deleted text" not in json.dumps(data)

    asyncio.run(scenario())


def test_delete_racing_an_sqlite_writer_cannot_recreate_content(tmp_path):
    import sqlite3

    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        memory.preferences.set_default("1", "tracking", "on")
        blocker = sqlite3.connect(memory.db_path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        writer = asyncio.create_task(
            memory.add_to_channel_memory(
                "20", {"author_id": "1", "content": "secret", "message_id": "100"}
            )
        )
        deleting = asyncio.create_task(
            asyncio.to_thread(delete_data, tmp_path, "1", preferences(tmp_path))
        )
        try:
            for _ in range(100):
                if not tracks_message_content(memory.preferences, "1"):
                    break
                await asyncio.sleep(0.005)
            assert not tracks_message_content(memory.preferences, "1")
            blocker.execute("ROLLBACK")
            await asyncio.wait_for(asyncio.gather(writer, deleting), 5)
            assert memory._db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
        finally:
            blocker.close()
            await memory.flush()

    asyncio.run(scenario())


def test_next_release_is_blocked_without_review_evidence():
    from scripts.check_release_readiness import release_blockers
    from pathlib import Path

    blockers = release_blockers(Path(__file__).resolve().parents[1])
    assert "Operator review is not approved" in blockers
    assert any("provider_inventory" in row for row in blockers)


def test_delete_data_revokes_manual_premium_grant_but_retains_short_usage_metadata(tmp_path):
    from message_quota import MessageQuota
    ledger = MessageQuota(tmp_path / "message_quota.sqlite3")
    ledger.configure("123", plan="premium")
    ledger.configure("456", plan="premium")
    ledger.charge("123", 200, 18000)
    result = delete_data(tmp_path, "123", preferences(tmp_path))
    assert "plan_grants" not in result["failed"]
    assert ledger.plan_for("123") == "free"
    assert ledger.plan_for("456") == "premium"
    assert ledger.status("123", 200, 18000)["used"] == 1
