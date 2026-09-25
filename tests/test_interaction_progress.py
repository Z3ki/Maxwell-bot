from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.maxwell_extras import interaction_progress as mod
from user_install import UserInstallSession


class _Message:
    def __init__(self, content: str = ""):
        self.id = 999
        self.content = content
        self.deleted = False
        self.flags = SimpleNamespace(ephemeral=False)

    async def edit(self, *, content=None, **_kwargs):
        if content is not None:
            self.content = content
        return self

    async def delete(self):
        self.deleted = True


class _Response:
    def __init__(self):
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def defer(self):
        self.deferred = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, **payload):
        msg = _Message(str(payload.get("content") or ""))
        self.sent.append((payload, msg))
        return msg


class _Channel:
    def __init__(self):
        self.id = 123
        self.name = "general"
        self.sent = []
        self.fail_send = False

    async def send(self, content=None, **payload):
        if self.fail_send:
            raise RuntimeError("channel send unavailable")
        msg = _Message(str(content or ""))
        recorded = {"content": content, **payload}
        self.sent.append((recorded, msg))
        return msg


class _Interaction:
    def __init__(self, iid: int):
        self.id = iid
        self.channel_id = 123
        self.channel = _Channel()
        self.guild = SimpleNamespace(id=456)
        self.response = _Response()
        self.followup = _Followup()
        self.original = _Message()
        self.edits = []
        self.original_deleted = False

    async def edit_original_response(self, *, content=None, **_kwargs):
        self.original.content = str(content or "")
        self.edits.append(self.original.content)
        return self.original

    async def original_response(self):
        return self.original

    async def delete_original_response(self):
        self.original_deleted = True
        await self.original.delete()


class _RacingInteraction(_Interaction):
    def __init__(self, iid: int):
        super().__init__(iid)
        self.final_edit_started = asyncio.Event()
        self.release_final_edit = asyncio.Event()

    async def edit_original_response(self, *, content=None, **_kwargs):
        text = str(content or "")
        self.original.content = text
        self.edits.append(text)
        if text == "final answer":
            self.final_edit_started.set()
            await self.release_final_edit.wait()
        return self.original


def _state(interaction: _Interaction, *, age: float = 0.0):
    state = mod._InteractionProgressState(interaction)
    state.started -= age
    mod._STATES[mod._key(interaction)] = state
    return state


def test_fast_answer_uses_original_interaction():
    async def run():
        mod._patch_session()
        interaction = _Interaction(1)
        state = _state(interaction)
        session = UserInstallSession(interaction)
        sent = await session.send("done")
        assert sent is interaction.original
        assert interaction.original.content == "done"
        assert interaction.followup.sent == []
        assert state.completed is True
        assert state.escalated is False

    asyncio.run(run())


def test_tool_escalation_keeps_status_and_replies_to_it():
    async def run():
        mod._patch_session()
        interaction = _Interaction(2)
        state = _state(interaction)
        session = UserInstallSession(interaction)
        await interaction.response.defer()
        await mod._mark_working(state)
        sent = await session.send("final answer")
        assert interaction.original.content == mod._STATUS_TEXT
        assert interaction.edits == [mod._STATUS_TEXT]
        assert interaction.original_deleted is False
        assert interaction.original.deleted is False
        assert interaction.channel.sent[0][0]["reference"] is interaction.original
        assert interaction.channel.sent[0][0]["mention_author"] is False
        assert sent is interaction.channel.sent[0][1]
        assert interaction.followup.sent == []
        assert state.escalated is True
        assert state.status_set is True
        assert state.completed is True

    asyncio.run(run())


def test_answer_between_five_and_ten_seconds_stays_original():
    async def run():
        mod._patch_session()
        interaction = _Interaction(3)
        state = _state(interaction, age=6.0)
        session = UserInstallSession(interaction)
        sent = await session.send("still fast enough")
        assert sent is interaction.original
        assert interaction.original.content == "still fast enough"
        assert interaction.followup.sent == []
        assert state.completed is True
        assert state.escalated is False

    asyncio.run(run())


def test_answer_after_ten_seconds_replies_to_working_status():
    async def run():
        mod._patch_session()
        interaction = _Interaction(4)
        state = _state(interaction, age=11.0)
        session = UserInstallSession(interaction)
        sent = await session.send("late answer")
        assert interaction.original.content == mod._STATUS_TEXT
        assert interaction.original_deleted is False
        assert interaction.channel.sent[0][0]["reference"] is interaction.original
        assert interaction.channel.sent[0][0]["content"] == "late answer"
        assert sent is interaction.channel.sent[0][1]
        assert interaction.followup.sent == []
        assert state.escalated is True

    asyncio.run(run())


def test_interaction_tool_status_is_kept_as_reply_parent():
    async def run():
        mod._patch_session()
        interaction = _Interaction(6)
        state = _state(interaction)
        session = UserInstallSession(interaction)
        await interaction.response.defer()

        await mod._note_interaction_tool(state, "web_search")
        assert interaction.original.content == "Maxwell is using: `web_search`"
        state.tool_status_last_edit -= 2.0
        await mod._note_interaction_tool(state, "fetch_url")
        assert interaction.original.content == (
            "Maxwell is using: `web_search`, `fetch_url`"
        )

        sent = await session.send("final answer")
        assert interaction.original_deleted is False
        assert interaction.channel.sent[0][0]["reference"] is interaction.original
        assert sent is interaction.channel.sent[0][1]
        assert interaction.followup.sent == []

    asyncio.run(run())


def test_slow_reply_falls_back_to_editing_status_when_channel_send_fails():
    async def run():
        mod._patch_session()
        interaction = _Interaction(7)
        interaction.channel.fail_send = True
        state = _state(interaction, age=11.0)
        session = UserInstallSession(interaction)

        sent = await session.send("late answer")
        assert interaction.channel.sent == []
        assert interaction.original_deleted is False
        assert interaction.original.content == "late answer"
        assert sent is interaction.original
        assert state.status_set is False

    asyncio.run(run())


def test_ephemeral_working_status_is_not_replied_to_publicly():
    async def run():
        mod._patch_session()
        interaction = _Interaction(8)
        interaction.original.flags.ephemeral = True
        state = _state(interaction, age=11.0)
        session = UserInstallSession(interaction)

        sent = await session.send("private late answer")
        assert interaction.channel.sent == []
        assert interaction.followup.sent == []
        assert interaction.original.content == "private late answer"
        assert sent is interaction.original

    asyncio.run(run())


def test_ephemeral_answer_does_not_replace_public_working_status():
    async def run():
        mod._patch_session()
        interaction = _Interaction(9)
        state = _state(interaction, age=11.0)
        session = UserInstallSession(interaction)

        sent = await session.send("private late answer", ephemeral=True)
        assert interaction.original.content == mod._STATUS_TEXT
        assert interaction.channel.sent == []
        assert interaction.followup.sent[0][0]["ephemeral"] is True
        assert sent is interaction.followup.sent[0][1]

    asyncio.run(run())


def test_timeout_cannot_overwrite_final_answer_at_boundary():
    async def run():
        mod._patch_session()
        interaction = _RacingInteraction(5)
        state = _state(interaction)
        session = UserInstallSession(interaction)

        send_task = asyncio.create_task(session.send("final answer"))
        await interaction.final_edit_started.wait()

        # Reproduce the timeout firing while the final original-response edit
        # is still in flight. The timeout must wait for the same state lock.
        working_task = asyncio.create_task(mod._mark_working(state))
        await asyncio.sleep(0)
        interaction.release_final_edit.set()

        sent = await send_task
        working_result = await working_task

        assert sent is interaction.original
        assert working_result is None
        assert interaction.original.content == "final answer"
        assert interaction.edits == ["final answer"]
        assert interaction.followup.sent == []
        assert state.completed is True
        assert state.escalated is False

    asyncio.run(run())
