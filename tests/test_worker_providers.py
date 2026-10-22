"""Independent worker routes never mix keys, accounts or deadlines."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot, _current_byok_tool_budget, _current_request_provider
from maxwell_core.providers.factory import openai_compat_provider
from maxwell_core.providers.models import ProviderPolicy, ProviderResult
from plugins.maxwell_extras.byok import CredentialVault, parse_model_support
from plugins.maxwell_extras.worker_providers import select_worker_provider
from premium_access import current_plan
from privacy_data import delete_data
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from provider_limits import (
    LIVE_TURN_TIMEOUT_DEFAULT_SECONDS,
    request_turn_timeout_seconds,
    worker_timeout_seconds,
)
from test_premium_task_workers import TASKS, setup


def routes(bot, root):
    extra = root / "plugins/maxwell_extras"
    bot._worker_byok_vault = CredentialVault(extra / "worker_byok.sqlite3", "ab" * 32)
    bot._byok_vault = CredentialVault(extra / "byok.sqlite3", "ab" * 32)
    bot._make_chat_provider = openai_compat_provider
    return bot._worker_byok_vault, bot._byok_vault


def save(vault, model="worker", **settings):
    vault.save(
        "123",
        "custom",
        model,
        "sk-private-worker-key-123456",
        base_url="https://example.com/v1",
        settings=parse_model_support(
            "text tools",
            "reasoning=on effort=high context=32000 max_tokens=8192 timeout=1200 turn_timeout=2400",
        )
        | settings,
    )


def test_dedicated_worker_byok_wins_and_honors_its_settings(tmp_path):
    bot, _, _, _ = setup(tmp_path)
    worker, chat = routes(bot, tmp_path)
    save(chat, "chat")
    save(worker, "worker")
    bot.config.AI_WORKER_MODEL = "operator"
    inherited = object()

    async def run():
        provider, owned = await select_worker_provider(bot, "123", inherited)
        try:
            assert owned and provider is not inherited and provider.model == "worker"
            assert provider.policy.sensitive_credentials
            assert not provider.policy.allow_provider_fallback
            assert provider.policy.max_request_seconds == 1200
            assert provider.policy.max_output_tokens == 8192
            assert provider.capabilities.context_window == 32000
            assert provider.reasoning_effort == "high"
            assert provider.turn_timeout_seconds == 2400
        finally:
            await provider.close()
        worker.delete("123")
        assert await select_worker_provider(bot, "123", inherited) == (inherited, False)
        provider, owned = await select_worker_provider(bot, "123")
        assert owned and provider.model == "chat"
        await provider.close()
        assert chat.status("123")["model"] == "chat"

    asyncio.run(run())


def test_empty_locked_vault_allows_operator_but_existing_locked_key_fails_closed(
    tmp_path,
):
    bot, msg, shell, tool = setup(tmp_path)
    worker, chat = routes(bot, tmp_path)
    worker._key = chat._key = None
    assert asyncio.run(select_worker_provider(bot, "123")) == (bot.ai_provider, False)
    worker._key = bytes.fromhex("ab" * 32)
    save(worker)
    worker._key = None
    result = asyncio.run(tool.run(msg, tasks=TASKS))
    assert "no task started" in result
    bot._generate_response.assert_not_awaited()
    shell.execute.assert_not_awaited()
    assert tool._active == 0


@pytest.mark.parametrize(
    "endpoint,key,expected_key",
    [
        ("", None, "chat-secret"),
        ("http://localhost:1234/v1", None, ""),
        ("", "", ""),
        ("https://workers.example/v1", "worker-secret", "worker-secret"),
    ],
)
def test_operator_route_credentials_never_leak_to_new_destination(
    tmp_path, endpoint, key, expected_key
):
    bot, _, _, _ = setup(tmp_path)
    captured = {}
    client = SimpleNamespace()
    bot._make_chat_provider = lambda **kwargs: captured.update(kwargs) or client
    bot.config = SimpleNamespace(
        AI_BASE_URL="https://chat.example/v1",
        AI_API_KEY="chat-secret",
        AI_WORKER_MODEL="slow-coder",
        AI_WORKER_BASE_URL=endpoint,
        AI_WORKER_API_KEY=key,
        AI_WORKER_MAX_OUTPUT_TOKENS=20000,
        AI_WORKER_CONTEXT_WINDOW=200000,
        AI_WORKER_DISABLE_REASONING=False,
        AI_WORKER_REASONING_EFFORT="max",
        AI_WORKER_REQUEST_TIMEOUT_SECONDS=1800,
        AI_WORKER_TURN_TIMEOUT_SECONDS=3600,
    )
    assert asyncio.run(select_worker_provider(bot, "123")) == (client, True)
    assert captured["api_key"] == expected_key
    assert captured["base_url"] == (endpoint or bot.config.AI_BASE_URL)
    assert captured["model"] == "slow-coder" and captured["reasoning_effort"] == "max"
    assert captured["context_window"] == 200000 and captured["max_tokens"] == 20000
    assert captured["policy"].max_request_seconds == 1800
    assert client.turn_timeout_seconds == 14400


def test_partial_operator_config_refuses_workers_without_breaking_chat(tmp_path):
    bot, msg, _, tool = setup(tmp_path)
    bot.config.AI_WORKER_BASE_URL = "http://localhost:1234/v1"
    assert "no task started" in asyncio.run(tool.run(msg, tasks=TASKS))
    bot._generate_response.assert_not_awaited()
    assert tool._active == 0


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "timeout"])
def test_owned_worker_client_is_closed_and_parent_context_restored(
    tmp_path, monkeypatch, outcome
):
    bot, msg, _, tool = setup(tmp_path)
    client = SimpleNamespace(
        policy=ProviderPolicy(
            sensitive_credentials=True, max_request_seconds=30, max_output_tokens=8192
        ),
        capabilities=SimpleNamespace(context_window=64000),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        "plugins.maxwell_extras.worker_providers.select_worker_provider",
        AsyncMock(return_value=(client, True)),
    )
    monkeypatch.setattr(
        "plugins.maxwell_extras.task_workers.worker_job_timeout_seconds",
        lambda bot, *a, **k: 0.01 if outcome == "timeout" else 30,
    )
    entered = asyncio.Event()

    async def generate(*args, **kwargs):
        assert _current_request_provider.get() is client
        assert _current_byok_tool_budget.get() is budget
        assert kwargs["provider"] is client and kwargs["max_tokens"] == 8192
        entered.set()
        if outcome in {"cancel", "timeout"}:
            await asyncio.Event().wait()
        if outcome == "error":
            raise OSError("secret error text")
        return ProviderResult("done")

    bot._generate_response.side_effect = generate
    parent = object()
    budget = {"calls": 0}

    async def run():
        parent_token = _current_request_provider.set(parent)
        budget_token = _current_byok_tool_budget.set(budget)
        try:
            if outcome == "cancel":
                task = asyncio.create_task(tool.run(msg, tasks=TASKS * 2))
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                report = await tool.run(msg, tasks=TASKS)
                assert "secret error" not in report
                assert ("deadline" in report) == (outcome == "timeout")
            assert _current_request_provider.get() is parent
        finally:
            _current_request_provider.reset(parent_token)
            _current_byok_tool_budget.reset(budget_token)
        client.close.assert_awaited_once()
        assert not tool._users and tool._active == 0

    asyncio.run(run())


def test_coordinator_delegates_general_task_and_worker_retains_actual_authorization(
    tmp_path,
):
    bot, msg, shell, tool = setup(tmp_path)
    bot.tools["delegate_task"] = tool
    token = current_plan.set(("123", "premium"))
    try:
        names = MaxwellBot._turn_tool_names(bot, "discord", msg)
        assert "delegate_task" in names and "shell" in names
        assert (
            MaxwellBot._authorize_tool_execution(
                bot, msg, "shell", shell, {"command": "pwd"}
            )
            is None
        )
        result = json.loads(
            asyncio.run(
                tool.run(
                    msg,
                    tasks=[
                        {"task": "Check workspace"}
                    ],
                )
            )
        )[0]
        assert result["status"] == "reported"
        shell.execute.assert_awaited_once()
    finally:
        current_plan.reset(token)
    assert "shell" in MaxwellBot._turn_tool_names(bot, "discord", msg)


def test_worker_without_tool_support_does_not_start_llm(tmp_path):
    bot, msg, _, tool = setup(tmp_path)
    bot.ai_provider.capabilities.native_tools = False
    result = json.loads(asyncio.run(tool.run(msg, tasks=TASKS)))[0]
    assert result["status"] == "unavailable"
    bot._generate_response.assert_not_awaited()


def test_worker_deadline_fits_parent_only_for_eligible_premium_requests(tmp_path):
    bot, _, _, _ = setup(tmp_path)
    bot.config.AI_WORKER_TURN_TIMEOUT_SECONDS = 2400
    assert worker_timeout_seconds(bot) == 2400
    assert request_turn_timeout_seconds(bot, premium=True) == LIVE_TURN_TIMEOUT_DEFAULT_SECONDS
    assert request_turn_timeout_seconds(bot, premium=False) == LIVE_TURN_TIMEOUT_DEFAULT_SECONDS
    bot._control["premium_task_workers_enabled"] = False
    assert request_turn_timeout_seconds(bot, premium=True) == LIVE_TURN_TIMEOUT_DEFAULT_SECONDS


def test_delete_removes_both_vaults_and_old_save_cannot_resurrect_key(tmp_path):
    bot, _, _, _ = setup(tmp_path)
    worker, chat = routes(bot, tmp_path)
    save(worker)
    save(chat)
    started = time.time() - 10
    result = delete_data(
        tmp_path,
        "123",
        UserPreferenceStore(tmp_path / "plugins/maxwell_extras/user_preferences.json"),
    )
    assert not result["failed"]
    for vault in (worker, chat):
        assert not vault.has_credential("123")
        with pytest.raises(ValueError, match="predates privacy deletion"):
            vault.save(
                "123",
                "custom",
                "old",
                "sk-old-key-123456",
                base_url="https://example.com/v1",
                started_at=started,
            )
        assert not CredentialVault(vault.path, "ab" * 32).has_credential("123")


def test_worker_byok_shares_parent_tool_budget_across_siblings(tmp_path, monkeypatch):
    from test_premium_task_workers import call

    bot, msg, shell, tool = setup(tmp_path)
    provider = SimpleNamespace(
        policy=ProviderPolicy(sensitive_credentials=True), close=AsyncMock()
    )
    monkeypatch.setattr(
        "plugins.maxwell_extras.worker_providers.select_worker_provider",
        AsyncMock(return_value=(provider, True)),
    )

    async def generate(messages, **kwargs):
        used_tool = any(row.get("role") == "tool" for row in messages)
        return ProviderResult("finished") if used_tool else call()

    bot._generate_response.side_effect = generate
    budget = {"calls": 23}
    token = _current_byok_tool_budget.set(budget)
    try:
        reports = json.loads(asyncio.run(tool.run(msg, tasks=TASKS * 2)))
    finally:
        _current_byok_tool_budget.reset(token)
    assert budget["calls"] == 24
    assert shell.execute.await_count == 1
    assert sorted(r["completed"][0]["status"] for r in reports) == ["error", "returned"]


def test_worker_carries_only_applicable_feature_instructions(tmp_path):
    from maxwell_core.prompts.manager import PromptManager
    from maxwell_core.prompts.component import PromptComponent

    bot, msg, _, tool = setup(tmp_path)
    bot.prompts = PromptManager()
    bot.prompts.register(
        PromptComponent(
            id="shell.rules",
            plugin="shell",
            text="Required scoped shell verification",
            requires_tools=("shell",),
        )
    )
    bot.prompts.register(
        PromptComponent(
            id="sites.rules",
            plugin="sites",
            text="UNRELATED WEBSITE RULE",
            requires_tools=("create_site",),
        )
    )
    bot.prompts.register(
        PromptComponent(
            id="shell.personality",
            plugin="shell",
            text="UNRELATED PERSONALITY",
            position="personality",
        )
    )
    bot.plugin_manager = SimpleNamespace(
        loaded_plugins={"shell", "sites"},
        is_plugin_enabled_for_user=lambda *a: True,
        all_plugin_tools={},
        get_available_tools=lambda **k: {},
    )
    bot._generate_response.side_effect = [ProviderResult("done")]
    asyncio.run(tool.run(msg, tasks=TASKS))
    prompt = json.dumps(bot._generate_response.await_args.args[0])
    assert "Required scoped shell verification" in prompt
    assert "UNRELATED" not in prompt


def test_worker_media_is_processed_as_transient_input_and_never_as_text(tmp_path):
    from tools import Tool
    from test_premium_task_workers import call

    class ImageTool(Tool):
        tool_name = "see_image"
        returns_result = True
        parameters = {"type": "object", "properties": {"url": {"type": "string"}}}

        def get_description(self):
            return "Inspect an image"

        async def execute(self, message, **kwargs):
            return "Image attached __IMAGE_B64__data:image/png;base64,YWJj__END_IMAGE_B64__"

    bot, msg, _, tool = setup(tmp_path)
    bot.ai_provider.capabilities.vision = True
    bot.tools["see_image"] = ImageTool(bot)
    received = []

    async def generate(messages, **kwargs):
        received.append((json.dumps(messages), list(kwargs["media"])))
        return (
            call("see_image", url="https://example.com/image.png")
            if len(received) == 1
            else ProviderResult("Analyzed")
        )

    bot._generate_response.side_effect = generate
    reports = asyncio.run(
        tool.run(
            msg,
            tasks=[
                {"task": "Analyze the supplied image"}
            ],
        )
    )
    assert received[0][1] == []
    assert received[1][1][0]["b64"] == "YWJj"
    assert all(
        "YWJj" not in text and "__IMAGE_B64__" not in text for text, _ in received
    )
    assert "YWJj" not in reports


def test_worker_cannot_widen_github_policy_or_write_credentials(tmp_path):
    from plugins.github_projects.impl import GitHubRepoTool
    from test_premium_task_workers import call

    bot, msg, _, tool = setup(tmp_path)
    github = SimpleNamespace(
        tool_name="github_repo",
        parameters=GitHubRepoTool.parameters,
        get_parameters=lambda: GitHubRepoTool.parameters,
        returns_result=True,
        get_description=lambda: "GitHub tasks",
        execute=AsyncMock(),
    )
    bot.tools["github_repo"] = github
    bot._generate_response.side_effect = [
        call("github_repo", action="policy_set", repo="owner/repo", mode="admin"),
        ProviderResult("continued"),
    ]
    report = json.loads(
        asyncio.run(
            tool.run(
                msg,
                tasks=[
                    {"task": "Inspect repo"}
                ],
            )
        )
    )[0]
    assert report["status"] == "reported"
    assert report["report"] == "continued"
    github.execute.assert_not_awaited()
    schema = bot._generate_response.await_args.kwargs["tools"][0]["function"][
        "parameters"
    ]
    assert (
        "policy_set" not in schema["properties"]["action"]["enum"]
        and "token" not in schema["properties"]
    )
    assert "policy_set" in GitHubRepoTool.parameters["properties"]["action"]["enum"]


def test_credentials_delete_racing_an_inflight_writer_cannot_resurrect_key(
    tmp_path, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import privacy

    bot, _, _, _ = setup(tmp_path)
    vault, _ = routes(bot, tmp_path)
    prefs = UserPreferenceStore(
        tmp_path / "plugins/maxwell_extras/user_preferences.json"
    )
    entered, suppressed, release = (threading.Event() for _ in range(3))
    original_check = privacy.owned_write_allowed
    original_delete = prefs.delete_personal

    def blocked_check(*args):
        entered.set()
        assert release.wait(5)
        return original_check(*args)

    def suppress(*args, **kwargs):
        result = original_delete(*args, **kwargs)
        suppressed.set()
        return result

    monkeypatch.setattr(privacy, "owned_write_allowed", blocked_check)
    monkeypatch.setattr(prefs, "delete_personal", suppress)
    with ThreadPoolExecutor(2) as pool:
        write = pool.submit(save, vault)
        assert entered.wait(5)
        deletion = pool.submit(delete_data, tmp_path, "123", prefs)
        try:
            assert suppressed.wait(5)
        finally:
            release.set()
        with pytest.raises(ValueError, match="predates privacy deletion"):
            write.result(timeout=5)
        assert not deletion.result(timeout=5)["failed"]
    assert not CredentialVault(vault.path, "ab" * 32).has_credential("123")


def test_worker_credential_deletion_failure_is_reported_without_secret_text(
    tmp_path, monkeypatch
):
    bot, _, _, _ = setup(tmp_path)
    vault, _ = routes(bot, tmp_path)
    save(vault)
    prefs = UserPreferenceStore(
        tmp_path / "plugins/maxwell_extras/user_preferences.json"
    )
    original = CredentialVault.delete

    def fail_worker(self, uid):
        if self.path.name == "worker_byok.sqlite3":
            raise OSError("secret-message-must-not-leak")
        return original(self, uid)

    monkeypatch.setattr(CredentialVault, "delete", fail_worker)
    result = delete_data(tmp_path, "123", prefs)
    assert result["failed"] == ["worker_byok"] and not result["complete"]
    assert "secret-message" not in json.dumps(result)


def test_model_configuration_snapshot_names_worker_model_without_keys_or_endpoints(
    tmp_path,
):
    from plugins.maxwell_extras.config_tools import settings_snapshot

    bot, msg, _, _ = setup(tmp_path)
    worker, chat = routes(bot, tmp_path)
    bot._user_preferences = UserPreferenceStore(
        tmp_path / "plugins/maxwell_extras/user_preferences.json"
    )
    save(worker, "separate-worker-model")
    save(chat, "separate-chat-model")
    result = asyncio.run(
        settings_snapshot(bot, SimpleNamespace(user=msg.author), "personal")
    )
    assert (
        result["worker_model"] == "separate-worker-model"
        and result["model"] == "separate-chat-model"
    )
    assert result["worker_model_source"] == "personal_worker"
    assert "sk-private" not in json.dumps(result) and "example.com" not in json.dumps(
        result
    )


def test_coordinator_github_schema_keeps_account_actions_only(tmp_path):
    from plugins.github_projects.impl import GitHubRepoTool

    bot, msg, _, tool = setup(tmp_path)
    github = SimpleNamespace(
        tool_name="github_repo",
        get_parameters=lambda: GitHubRepoTool.parameters,
        get_description=lambda: "GitHub tasks",
    )
    bot.tools.update({"github_repo": github, "delegate_task": tool})
    bot._native_tools_enabled = lambda: True
    token = current_plan.set(("123", "premium"))
    try:
        schemas = MaxwellBot._build_openai_tools(bot, message=msg)
        params = next(
            s["function"]["parameters"]
            for s in schemas
            if s["function"]["name"] == "github_repo"
        )
        assert (
            "auth" in params["properties"]["action"]["enum"]
            and "run" not in params["properties"]["action"]["enum"]
        )
        assert (
            MaxwellBot._authorize_tool_execution(
                bot, msg, "github_repo", github, {"action": "run"}
            )
            is None
        )
        assert "run" in GitHubRepoTool.parameters["properties"]["action"]["enum"]
    finally:
        current_plan.reset(token)
