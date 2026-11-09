"""Deterministic sandbox/site regressions without Docker or network access."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import site_server
from plugins.sites.impl import CreateSiteTool, EditSiteTool
from tooling import helpers


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("voice", [False, True])
def test_channel_lock_preserves_unrelated_everyone_overwrites(monkeypatch, locked, voice):
    class Channel:
        def __init__(self):
            self.guild = SimpleNamespace(default_role=object())
            self.id = 9
            self.mention = "<#9>"
            self.overwrite = discord.PermissionOverwrite(
                view_channel=False, read_message_history=False,
                manage_messages=True, send_messages=True,
                send_messages_in_threads=True, connect=True,
            )
            self.set_permissions = AsyncMock()

        def overwrites_for(self, target):
            assert target is self.guild.default_role
            return self.overwrite

    class VoiceChannel(Channel):
        pass

    monkeypatch.setattr(helpers.discord, "VoiceChannel", VoiceChannel)
    channel = VoiceChannel() if voice else Channel()
    assert asyncio.run(helpers._lock_target(channel, locked, "test")) == ""
    overwrite = channel.set_permissions.call_args.kwargs["overwrite"]
    assert overwrite.view_channel is False
    assert overwrite.read_message_history is False
    assert overwrite.manage_messages is True
    assert overwrite.connect is (False if locked else None) if voice else overwrite.connect is True
    if not voice:
        assert overwrite.send_messages is (False if locked else None)
        assert overwrite.send_messages_in_threads is (False if locked else None)


@pytest.mark.parametrize("raw", [b"{", b"[]", b'{"other": 5}', b"\xff"])
def test_site_creation_never_replaces_malformed_registry(tmp_path, raw):
    site_dir = tmp_path / "public" / "bot"
    data_dir = tmp_path / "data"
    site_dir.mkdir(parents=True)
    data_dir.mkdir()
    path = data_dir / "sites.json"
    path.write_bytes(raw)
    bot = SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_SITE_DIR=str(site_dir), DATA_DIR=str(data_dir),
            MAXWELL_PUBLIC_BASE_URL="https://example.invalid",
        ),
        _sites={}, _load_sites=lambda quiet=True: None,
        _is_admin=lambda _uid: False, _control={}, control={}, tools={},
    )
    message = SimpleNamespace(author=SimpleNamespace(id=42, display_name="tester"))
    result = asyncio.run(CreateSiteTool(bot).execute(
        message, name="new-site", title="New", body="<h1>New</h1>",
    ))
    assert result.startswith("Error:")
    assert path.read_bytes() == raw
    assert "new-site" not in bot._sites

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        EditSiteTool(bot)._save_entry("existing", {"user_id": "42"})
    assert path.read_bytes() == raw


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("runner", ["site", "shell"])
def test_docker_cleanup_drains_pipes_before_waiting(monkeypatch, cancelled, runner):
    class Process:
        returncode = None
        drained = False
        reaped = False
        killed = False
        calls = 0

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                if cancelled:
                    raise asyncio.CancelledError
                raise asyncio.TimeoutError
            self.drained = True
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            assert self.drained, "waiting without readers deadlocks on full pipes"
            self.reaped = True
            return self.returncode

    process = Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    call = site_server._docker if runner == "site" else helpers._run_docker_cmd
    expected = asyncio.CancelledError if cancelled else (
        site_server.SiteServerError if runner == "site" else asyncio.TimeoutError
    )
    with pytest.raises(expected):
        asyncio.run(call("inspect", "fake", timeout=1))
    assert process.killed
    assert process.drained
    assert process.reaped
