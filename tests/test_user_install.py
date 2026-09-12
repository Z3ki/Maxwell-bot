"""Admin-only Discord user-install (/maxwell)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot
from user_install import (
    USER_INSTALL_COMMAND_NAME,
    USER_INSTALL_COMMANDS,
    USER_INSTALL_MESSAGE_CAP,
    UserInstallMessageAdapter,
    handle_user_install_interaction,
    is_user_install_command,
    is_user_install_message,
    parse_user_install_command,
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
    assert len(USER_INSTALL_COMMANDS) == 1
    cmd = USER_INSTALL_COMMANDS[0]
    assert cmd["name"] == USER_INSTALL_COMMAND_NAME
    assert cmd["integration_types"] == [1]
    assert cmd["contexts"] == [0, 1, 2]


def test_parse_prompt_and_image():
    interaction = _interaction(prompt="look", image=True)
    prompt, atts = parse_user_install_command(interaction)
    assert prompt == "look"
    assert atts[0].filename == "pic.png"
    assert atts[0].url.endswith("pic.png")


def test_is_user_install_command_filters_name():
    assert is_user_install_command(_interaction())
    assert not is_user_install_command(_interaction(name="help"))


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
