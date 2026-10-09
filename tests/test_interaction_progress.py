from __future__ import annotations

import asyncio
import io
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

    async def defer(self, **_kwargs):
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
            # A refused Discord upload reads the body before returning 403.
            file = payload.get("file")
            fp = getattr(file, "fp", None)
            if fp is not None:
                fp.read()
            for item in payload.get("files") or []:
                item_fp = getattr(item, "fp", None)
                if item_fp is not None:
                    item_fp.read()
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


def test_send_message_does_not_create_or_escalate_interaction_progress():
    async def run():
        interaction = _Interaction(901)
        state = _state(interaction)
        await mod._note_interaction_tool(state, "send_message")
        await mod._note_interaction_tool(state, " SEND_MESSAGE ")
        assert state.tool_calls == []
        assert state.escalated is False
        assert interaction.edits == []
        await mod._note_interaction_tool(state, "web_search")
        state.tool_status_last_edit -= 2.0
        await mod._note_interaction_tool(state, "send_message")
        await mod._flush_tool_status(state)
        assert state.tool_calls == ["web_search"]
        assert interaction.original.content == "Maxwell is using: `web_search`"
        await mod._clear_working_status(state)
    asyncio.run(run())


def test_interaction_progress_renderer_filters_hidden_tools_defensively():
    assert mod._tool_status_text(["send_message"]) == "working on it…"
    assert mod._tool_status_text(["web_search", "send_message", "fetch_url"]) == (
        "Maxwell is using: `web_search`, `fetch_url`"
    )


def test_fast_answer_uses_original_interaction():
    async def run():
        mod._patch_session()
        interaction = _Interaction(1)
        state = _state(interaction)
        session = UserInstallSession(interaction, visibility="public")
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
        session = UserInstallSession(interaction, visibility="public")
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
        session = UserInstallSession(interaction, visibility="public")
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
        session = UserInstallSession(interaction, visibility="public")
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
        session = UserInstallSession(interaction, visibility="public")
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


def test_failed_channel_file_send_rewinds_before_the_followup():
    """A rejected channel upload must not make the follow-up a 0-byte file."""

    class _Clip:
        def __init__(self, data: bytes):
            self.fp = io.BytesIO(data)
            self._original_pos = 0
            self.filename = "voice.mp3"

        def reset(self, *, seek=True):
            if seek:
                self.fp.seek(self._original_pos)

    class _DrainingChannel(_Channel):
        async def send(self, content=None, **payload):
            upload = payload.get("file")
            if upload is not None:
                upload.fp.read()
            raise RuntimeError("missing access")

    class _ReadingFollowup(_Followup):
        def __init__(self):
            super().__init__()
            self.bodies = []

        async def send(self, **payload):
            upload = payload.get("file")
            self.bodies.append(upload.fp.read() if upload is not None else b"")
            return await super().send(**payload)

    async def run():
        mod._patch_session()
        interaction = _Interaction(11)
        interaction.channel = _DrainingChannel()
        interaction.followup = _ReadingFollowup()
        _state(interaction, age=11.0)
        session = UserInstallSession(interaction, visibility="public")
        clip = _Clip(b"ID3real-audio")
        await session.send(file=clip)
        assert interaction.followup.bodies == [b"ID3real-audio"]

    asyncio.run(run())


def test_slow_reply_falls_back_to_editing_status_when_channel_send_fails():
    async def run():
        mod._patch_session()
        interaction = _Interaction(7)
        interaction.channel.fail_send = True
        state = _state(interaction, age=11.0)
        session = UserInstallSession(interaction, visibility="public")

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
        _state(interaction, age=11.0)
        session = UserInstallSession(interaction, visibility="public")

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
        _state(interaction, age=11.0)
        session = UserInstallSession(interaction, visibility="private")

        sent = await session.send("private late answer", ephemeral=True)
        assert interaction.original.content == mod._STATUS_TEXT
        assert interaction.channel.sent == []
        assert interaction.followup.sent[0][0]["ephemeral"] is True
        mentions = interaction.followup.sent[0][0]["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert sent is interaction.followup.sent[0][1]

    asyncio.run(run())


def test_timeout_cannot_overwrite_final_answer_at_boundary():
    async def run():
        mod._patch_session()
        interaction = _RacingInteraction(5)
        state = _state(interaction)
        session = UserInstallSession(interaction, visibility="public")

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


def test_private_session_never_sends_slow_answer_to_channel():
    async def run():
        mod._patch_session()
        interaction = _Interaction(987)
        _state(interaction, age=11.0)
        session = UserInstallSession(interaction, visibility="private")
        # Even missing or misleading status flags cannot publish a private answer.
        await session.send("private result")
        assert interaction.channel.sent == []
        assert interaction.followup.sent[-1][0]["ephemeral"] is True
        assert interaction.followup.sent[-1][0]["content"] == "private result"

    asyncio.run(run())
