"""Exercise the conversation service through its explicit transport interface."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from context_budget import allocate
from maxwell_core.prompts.conversation import (
    ConversationPromptBuilder,
    ConversationPromptHooks,
)


class Memory:
    def __init__(self, *, failed=False, cold=False):
        self.failed = failed
        self.cold = cold
        self.requests = []
        self.rows = [
            {
                "author": "Alice",
                "author_id": "42",
                "content": "older message",
                "message_id": "5",
            },
            {
                "author": "Maxwell",
                "author_id": "1",
                "content": "older reply",
                "message_id": "6",
            },
        ]

    async def get_channel_memory(self, channel, *, requester):
        self.requests.append(requester)
        return self.rows

    def get_long_term_memory(self, requester):
        self.requests.append(requester)
        return [{"content": "cold-start authorized fact"}]

    async def rag_search(self, query, *, kinds, requester, **kwargs):
        self.requests.append(requester)
        if self.failed:
            raise RuntimeError("backend unavailable")
        if self.cold:
            return []
        if kinds == ["ltm"]:
            return [
                {"kind": "ltm", "similarity": 0.8, "content": "authorized durable fact"}
            ]
        return [
            {
                "kind": "message",
                "similarity": 0.8,
                "content": "recalled message",
                "author": "Alice",
                "timestamp": "2026-09-01T12:00:00Z",
            }
        ]

    async def recall_web_results(self, query, *, requester, **kwargs):
        self.requests.append(requester)
        if self.cold:
            return []
        return [
            {
                "similarity": 0.8,
                "url": "https://example.com/source",
                "title": "Source",
                "content": "Source\nSource\nhistorical page text",
                "timestamp": "2026-09-01T12:00:00Z",
            }
        ]

    async def get_relevant_shared_context(self, *, requester, **kwargs):
        self.requests.append(requester)
        if self.failed:
            raise RuntimeError("backend unavailable")
        return [
            {"scope": "user:42", "importance": 5, "content": "authorized shared fact"}
        ]


def service(*, failed=False, cold=False, short=False, private=False, **controls):
    memory = Memory(failed=failed, cold=cold)
    host = SimpleNamespace(
        _control={
            "base_personality": "test",
            "music_context_enabled": False,
            "emoji_context_enabled": False,
            "entity_memory_enabled": False,
            "long_term_memory_enabled": True,
            "cross_context_enabled": True,
            **controls,
        },
        _base_knowledge="CORE INSTRUCTIONS",
        _discord_chat_protocol="CHAT PROTOCOL",
        _drugged_until={},
        _recent_users={},
        _guild_emojis={},
        _is_admin=lambda _user: False,
        _is_short_live_turn=lambda *_args: short,
        _shared_fact_relevant=lambda *_args: True,
        _tool_system_prompt=lambda **_kwargs: "",
        _reply_parent_context_lines=lambda _message: ["trusted reply-parent context"],
        _conversation_watch_prompt=lambda *_args: ["watch-only context"],
        memory=memory,
        user=SimpleNamespace(id=1, display_name="Maxwell"),
        bot_name="Maxwell",
        config=SimpleNamespace(RAG_WEB_STORE_ENABLED=True),
    )

    async def entity(*_args, **_kwargs):
        return None, []

    hooks = ConversationPromptHooks(
        apply_prompt_budget=lambda _host, messages: messages,
        context_budget_plan=lambda *_args: allocate(80000),
        entity_profile_for=entity,
        graph_prompt_block=lambda *_args, **_kwargs: "",
        message_content_chars=lambda row: len(str(row.get("content") or "")),
        prompt_budget_chars=lambda _host: 100000,
        render_entity_block=lambda *_args: "",
        self_repetition_note=lambda *_args: "",
        thread_prompt_block=lambda *_args: "",
        trim_middle=lambda text, limit: text[:limit],
    )
    message = SimpleNamespace(
        id=9,
        author=SimpleNamespace(id=42, display_name="Alice", bot=False),
        channel=SimpleNamespace(id=20, name="chat"),
        guild=SimpleNamespace(id=30, name="Guild"),
        mentions=[],
        response_visibility="private" if private else "public",
    )
    return ConversationPromptBuilder(host, hooks), message, memory


def build(builder, message, **kwargs):
    return asyncio.run(builder.build(message, "latest request", **kwargs))


def test_retrieved_facts_are_scoped_and_cached_web_is_explicitly_historical():
    builder, message, memory = service()
    before = copy.deepcopy(memory.rows)
    messages = build(builder, message)
    all_text = "\n".join(row["content"] for row in messages)
    assert "authorized durable fact" in all_text
    assert "recalled message" in all_text
    assert "authorized shared fact" in all_text
    assert "historical page text" in all_text
    assert "not verified current facts" in all_text
    assert "https://example.com/source" in all_text
    assert memory.rows == before
    assert len(memory.requests) >= 5
    assert all(
        (req.user_id, req.channel_id, req.guild_id, req.is_admin)
        == ("42", "20", "30", False)
        for req in memory.requests
    )
    assert messages[0]["content"].startswith("CORE INSTRUCTIONS")
    transcript_index = next(
        index
        for index, row in enumerate(messages)
        if row["content"].startswith("<previous_conversation>")
    )
    facts_index = next(
        index
        for index, row in enumerate(messages)
        if "authorized durable fact" in row["content"]
    )
    assert transcript_index < facts_index < len(messages) - 1
    assert "latest request" in messages[-1]["content"]
    assert "trusted reply-parent context" in messages[-1]["content"]


def test_cold_embeddings_fall_back_to_authorized_recent_facts():
    builder, message, _ = service(cold=True)
    messages = build(builder, message)
    assert any("cold-start authorized fact" in row["content"] for row in messages)
    assert not any("Earlier web results" in row["content"] for row in messages)


def test_optional_memory_failures_preserve_instructions_history_and_live_request():
    builder, message, _ = service(failed=True)
    messages = build(builder, message)
    assert messages[0]["content"].startswith("CORE INSTRUCTIONS")
    assert any("older message" in row["content"] for row in messages)
    assert "latest request" in messages[-1]["content"]
    assert not any("backend unavailable" in row["content"] for row in messages)


@pytest.mark.parametrize("short", [False, True])
def test_disabled_lookup_tiers_do_not_call_memory_backends(short):
    builder, message, memory = service(
        short=short, long_term_memory_enabled=False, cross_context_enabled=False
    )
    messages = build(builder, message)
    assert len(memory.requests) == 1  # The authorized this-channel transcript only.
    assert not any("authorized durable fact" in row["content"] for row in messages)
    assert "latest request" in messages[-1]["content"]


def test_short_ambient_turn_skips_optional_lookups_but_keeps_this_chat():
    builder, message, memory = service(short=True)
    messages = build(builder, message)
    assert len(memory.requests) == 1
    assert any("older message" in row["content"] for row in messages)


@pytest.mark.parametrize("private", [False, True])
def test_private_response_does_not_inherit_ambient_watch_instructions(private):
    builder, message, _ = service(private=private)
    messages = build(builder, message)
    assert (
        any("watch-only context" in row["content"] for row in messages) is not private
    )


def test_cancelling_scoped_history_read_stops_prompt_construction():
    builder, message, _ = service()

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    builder.host.memory.get_channel_memory = cancelled
    with pytest.raises(asyncio.CancelledError):
        build(builder, message)


def test_media_request_keeps_media_manifest_in_live_turn():
    builder, message, _ = service()
    messages = build(
        builder, message, has_media=True, media_summary="current media manifest"
    )
    assert "current media manifest" in messages[-1]["content"]
    assert any("Multimodal:" in row["content"] for row in messages)
