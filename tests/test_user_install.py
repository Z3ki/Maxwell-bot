"""Admin-only Discord user-install (/maxwell)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot
from user_install import (
    USER_INSTALL_COMMAND_NAME,
    USER_INSTALL_COMMANDS,
    USER_INSTALL_MESSAGE_ASK,
    USER_INSTALL_MESSAGE_CAP,
    USER_INSTALL_MESSAGE_SUMMARIZE,
    UserInstallMessageAdapter,
    build_user_install_turn,
    handle_user_install_interaction,
    is_user_install_command,
    is_user_install_message,
    merge_user_install_history,
    parse_user_install_command,
    snapshot_channel_history,
)


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def defer(self):
        self.deferred = True

    async def send_message(self, content, ephemeral=False):
        self.messages.append({"content": content, "ephemeral": ephemeral})


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, file=None, ephemeral=False, **kwargs):
        msg = SimpleNamespace(id=len(self.sent) + 1, content=content, file=file)

        async def edit(*, content=None, **_kwargs):
            if content is not None:
                msg.content = content
            return msg

        msg.edit = edit
        self.sent.append(msg)
        return msg


def _interaction(*, user_id=1, prompt="hello", name="maxwell", image=None):
    options = [{"name": "prompt", "type": 3, "value": prompt}]
    resolved = {}
    if image is not None:
        options.append({"name": "image", "type": 11, "value": "att1"})
        resolved["attachments"] = {
            "att1": {
                "id": "att1",
                "filename": "pic.png",
                "url": "https://cdn.discordapp.com/pic.png",
                "content_type": "image/png",
                "size": 12,
            }
        }
    return SimpleNamespace(
        id=99,
        type=2,
        channel_id=555,
        channel=SimpleNamespace(name="general", id=555),
        guild=None,
        user=SimpleNamespace(
            id=user_id, name="admin", display_name="admin", bot=False
        ),
        data={"name": name, "options": options, "resolved": resolved},
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def test_command_payload_is_user_install_only():
    assert len(USER_INSTALL_COMMANDS) == 4
    kinds = {(c["name"], c["type"]) for c in USER_INSTALL_COMMANDS}
    assert (USER_INSTALL_COMMAND_NAME, 1) in kinds
    assert (USER_INSTALL_MESSAGE_ASK, 3) in kinds
    assert (USER_INSTALL_MESSAGE_SUMMARIZE, 3) in kinds
    assert (USER_INSTALL_MESSAGE_ASK, 2) in kinds
    assert all(c["integration_types"] == [1] for c in USER_INSTALL_COMMANDS)


def test_parse_prompt_and_image():
    interaction = _interaction(prompt="look", image=True)
    prompt, atts = parse_user_install_command(interaction)
    assert prompt == "look"
    assert atts[0].filename == "pic.png"
    assert atts[0].url.endswith("pic.png")


def test_is_user_install_command_filters_name():
    assert is_user_install_command(_interaction())
    assert is_user_install_command(_interaction(name=USER_INSTALL_MESSAGE_ASK))
    assert not is_user_install_command(_interaction(name="help"))


def test_message_command_uses_target_as_reply_parent():
    interaction = _interaction(name=USER_INSTALL_MESSAGE_ASK, prompt="unused")
    interaction.data = {
        "name": USER_INSTALL_MESSAGE_ASK,
        "type": 3,
        "target_id": "88",
        "resolved": {
            "messages": {
                "88": {
                    "id": "88",
                    "content": "look at this bug",
                    "author": {
                        "id": "7",
                        "username": "alice",
                        "global_name": "Alice",
                    },
                    "attachments": [
                        {
                            "id": "a1",
                            "filename": "shot.png",
                            "url": "https://cdn.discordapp.com/shot.png",
                            "content_type": "image/png",
                            "size": 4,
                        }
                    ],
                    "embeds": [{"title": "trace", "description": "boom"}],
                }
            }
        },
    }
    turn = build_user_install_turn(interaction)
    assert turn is not None
    assert turn["prompt"] == "Respond to this message."
    parent = turn["reference"].resolved
    assert parent.content.startswith("look at this bug")
    assert "boom" in parent.content
    assert parent.attachments[0].filename == "shot.png"
    from datetime import datetime

    from utils import render_discord_context_text

    interaction.data["resolved"]["messages"]["88"]["timestamp"] = (
        "2026-09-12T14:19:38.123000+00:00"
    )
    parent = build_user_install_turn(interaction)["reference"].resolved
    assert isinstance(parent.created_at, datetime)
    rendered = render_discord_context_text(parent, parent.content)
    assert "2026-09-12 14:19:38 UTC" in rendered


def test_summarize_and_user_command_prompts():
    msg = _interaction(name=USER_INSTALL_MESSAGE_SUMMARIZE)
    msg.data = {
        "name": USER_INSTALL_MESSAGE_SUMMARIZE,
        "type": 3,
        "target_id": "1",
        "resolved": {
            "messages": {
                "1": {
                    "id": "1",
                    "content": "long post",
                    "author": {"id": "2", "username": "bob"},
                }
            }
        },
    }
    assert build_user_install_turn(msg)["prompt"] == "Summarize this message."
    user_cmd = _interaction(name=USER_INSTALL_MESSAGE_ASK)
    user_cmd.data = {
        "name": USER_INSTALL_MESSAGE_ASK,
        "type": 2,
        "target_id": "9",
        "resolved": {
            "users": {
                "9": {
                    "id": "9",
                    "username": "carol",
                    "global_name": "Carol",
                }
            }
        },
    }
    turn = build_user_install_turn(user_cmd)
    assert "Carol" in turn["prompt"]
    assert turn["mentions"][0].id == 9


def test_merge_user_install_history_prefers_unseen_snapshot():
    memory = [{"message_id": "1", "content": "stored"}]
    extra = [
        {"message_id": "1", "content": "dup"},
        {"message_id": "2", "content": "live"},
    ]
    merged = merge_user_install_history(memory, extra)
    assert [row["message_id"] for row in merged] == ["2", "1"]
    assert merge_user_install_history([], extra) == extra


def test_snapshot_channel_history_reads_async_history():
    class Chan:
        def __init__(self):
            self.id = 555

        def history(self, *, limit=25):
            async def gen():
                # Discord history() is newest-first.
                for i in (2, 1, 0):
                    yield SimpleNamespace(
                        id=i,
                        content=f"line {i}",
                        author=SimpleNamespace(
                            id=3, display_name="n", name="n", bot=False
                        ),
                        attachments=[],
                        created_at=None,
                    )

            return gen()

    interaction = _interaction()
    interaction.channel = Chan()
    rows = asyncio.run(snapshot_channel_history(None, interaction))
    assert [row["content"] for row in rows] == ["line 0", "line 1", "line 2"]


def test_adapter_send_caps_at_six():
    interaction = _interaction()
    asyncio.run(interaction.response.defer())
    msg = UserInstallMessageAdapter(interaction, "hi")
    assert is_user_install_message(msg)

    async def run():
        for i in range(USER_INSTALL_MESSAGE_CAP + 2):
            await msg.channel.send(f"chunk {i}")
        return interaction.followup.sent

    sent = asyncio.run(run())
    assert len(sent) == USER_INSTALL_MESSAGE_CAP
    last = sent[-1]
    assert "chunk 5" in last.content
    assert "chunk 6" in last.content
    assert "chunk 7" in last.content


def test_handle_rejects_non_admin():
    interaction = _interaction(user_id=2)
    bot = SimpleNamespace(
        _is_admin=lambda uid: str(uid) == "1",
        _spawn_detached=lambda coro: (_ for _ in ()).throw(AssertionError("spawned")),
        on_message=lambda message: (_ for _ in ()).throw(AssertionError("ran")),
    )
    claimed = asyncio.run(handle_user_install_interaction(bot, interaction))
    assert claimed is True
    assert interaction.response.messages
    assert "admins" in interaction.response.messages[0]["content"].lower()
    assert interaction.response.messages[0]["ephemeral"] is True


def test_handle_defers_and_spawns_for_admin():
    interaction = _interaction(user_id=1, prompt="ping me")
    spawned = []

    async def on_message(message):
        return None

    def spawn(coro):
        spawned.append(coro.cr_frame.f_locals["message"])
        coro.close()

    bot = SimpleNamespace(
        _is_admin=lambda uid: str(uid) == "1",
        _spawn_detached=spawn,
        on_message=on_message,
    )
    claimed = asyncio.run(handle_user_install_interaction(bot, interaction))
    assert claimed is True
    assert interaction.response.deferred is True
    assert spawned
    assert is_user_install_message(spawned[0])
    assert spawned[0].content == "ping me"


def test_directly_addressed_for_user_install():
    bot = SimpleNamespace(user=SimpleNamespace(id=111))
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._self_ids = lambda: {111}
    msg = UserInstallMessageAdapter(_interaction(), "hi")
    msg.guild = SimpleNamespace(id=1)
    assert bot._directly_addressed(msg) is True


def test_policy_skips_channel_allowlist_for_user_install():
    bot = SimpleNamespace(
        _control={
            "bot_enabled": True,
            "blocked_channels": ["555"],
            "allowed_channels": ["999"],
            "ignore_users": [],
        },
        _blacklist=set(),
    )
    bot._is_admin = lambda uid: True
    bot._solo_blocks = lambda message: True
    bot._queued_request_policy_reason = MaxwellBot._queued_request_policy_reason.__get__(
        bot
    )
    msg = UserInstallMessageAdapter(_interaction(), "hi")
    assert bot._queued_request_policy_reason(msg) == ""


def test_handle_ignores_other_commands():
    interaction = _interaction(name="wiki")
    bot = SimpleNamespace(_is_admin=lambda uid: True)
    assert asyncio.run(handle_user_install_interaction(bot, interaction)) is False
