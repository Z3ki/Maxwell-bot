"""User-visible public panels, stale controls and private interaction outcomes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import user_install as ui

from plugins.music.service import Track
from plugins.music.tools import MusicControlTool
from plugins.music.ui import install, uninstall, state_embed, update_panel
from test_music import fixture


class TextChannel:
    def __init__(self, channel_id=200):
        self.id = channel_id
        self.messages = []

    async def send(self, **kwargs):
        message = SimpleNamespace(id=len(self.messages) + self.id * 100, channel=self, payload=kwargs)

        async def edit(**changes):
            message.payload.update(changes)
            return message

        message.edit = AsyncMock(side_effect=edit)
        self.messages.append(message)
        return message


def interaction(message, action, **options):
    return SimpleNamespace(guild=message.guild, user=message.author, channel=message.channel,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        data={'name': 'music', 'options': [{'name': action, 'options': [
            {'name': name, 'value': value} for name, value in options.items()]}]})


def music_handler():
    return next(handler for _, name, handler in ui._INTERACTION_HANDLERS if name == 'music')


def feedback(request):
    return request.followup.send.call_args.kwargs['embeds'][0].description


def test_player_updates_one_panel_through_queue_pause_stop_and_disconnect(tmp_path):
    service, _, message, _ = fixture(tmp_path)
    message.channel = TextChannel()

    async def journey():
        service.panel_update = update_panel
        await service.execute(message, 'play', query='first')
        session = service.sessions[10]
        panel = session.panel
        initial_toggle = panel.payload['view'].children[0].label
        await update_panel(session)
        await service.execute(message, 'pause')
        assert session.panel is panel
        assert panel.payload['view'].children[0].label != initial_toggle
        await service.execute(message, 'play', query='next')
        assert 'next' in panel.payload['embed'].fields[-1].value
        await service.execute(message, 'resume')
        assert panel.payload['view'].children[0].label == initial_toggle
        message.author.guild_permissions.manage_guild = True
        await service.execute(message, 'stop')
        assert panel.payload['view'].children[0].disabled
        assert panel.payload['view'].children[1].disabled
        await service.execute(message, 'leave')
        assert panel.payload['view'] is None
        assert message.channel.messages == [panel]

    asyncio.run(journey())


def test_deleted_panel_and_text_channel_move_publish_fresh_controls(tmp_path):
    service, _, message, _ = fixture(tmp_path)
    message.channel = TextChannel()

    async def journey():
        service.panel_update = update_panel
        await service.execute(message, 'play', query='first')
        session = service.sessions[10]
        deleted = session.panel
        deleted.edit.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Deleted')
        await service.execute(message, 'volume', value=65)
        assert session.panel is not deleted
        assert '65%' in session.panel.payload['embed'].fields[-2].value
        old = session.panel
        message.channel = TextChannel(300)
        await service.execute(message, 'play', query='second')
        assert old.payload['view'] is None
        assert session.panel is message.channel.messages[-1]

    asyncio.run(journey())


def test_batch_ai_autoplay_permissions_and_stale_controls_share_session(tmp_path):
    service, backend, message, _ = fixture(tmp_path)
    message.channel = TextChannel()

    async def journey():
        install(service.bot, service)
        try:
            handler = music_handler()
            request = interaction(message, 'batch', input='first\nsecond\nthird', mode='queue')
            assert await handler(service.bot, request)
            session = service.sessions[10]
            assert session.current.title == 'first'
            assert [t.title for t in session.queue] == ['second', 'third']
            old = session.panel
            await MusicControlTool(service.bot, service).execute(message, action='play', query='AI song')
            button = interaction(message, 'status')
            button.data = {'custom_id': 'maxwell_music:10:skip'}
            button.message = old
            await handler(service.bot, button)
            assert session.current.title == 'second'
            message.channel = TextChannel(300)
            await handler(service.bot, interaction(message, 'status'))
            await handler(service.bot, button)
            assert session.current.title == 'second'
            assert button.followup.send.call_args.kwargs['ephemeral'] is True
            service.settings['10'] = {'listener_controls': False}
            denied = interaction(message, 'autoplay', enabled=True)
            await handler(service.bot, denied)
            assert not session.autoplay
            service.settings['10'] = {'listener_controls': True}
            enabled = interaction(message, 'autoplay', enabled=True)
            await handler(service.bot, enabled)
            assert session.autoplay
            button.message = session.panel
            button.data = {'custom_id': 'maxwell_music:10:autoplay'}
            await handler(service.bot, button)
            assert not session.autoplay
            invalid = interaction(message, 'batch', input='\n'.join(f'song {n}' for n in range(11)))
            before = list(session.queue)
            await handler(service.bot, invalid)
            assert session.queue == before
        finally:
            uninstall()

    asyncio.run(journey())


def test_status_updates_panel_and_failed_publish_keeps_private_feedback(tmp_path):
    service, _, message, _ = fixture(tmp_path)
    message.channel = TextChannel()

    async def journey():
        install(service.bot, service)
        try:
            handler = music_handler()
            await service.execute(message, 'play', query='first')
            session = service.sessions[10]
            old = session.panel
            request = interaction(message, 'status')
            await handler(service.bot, request)
            assert session.panel is old
            assert message.channel.messages == [old]
            old.edit.side_effect = discord.Forbidden(
                SimpleNamespace(status=403, reason='Forbidden'), 'No access')
            message.channel.send = AsyncMock(side_effect=discord.Forbidden(
                SimpleNamespace(status=403, reason='Forbidden'), 'No access'))
            paused = interaction(message, 'pause')
            await handler(service.bot, paused)
            assert session.paused
            assert session.panel is None
            private = interaction(message, 'status')
            await handler(service.bot, private)
            assert private.followup.send.call_args.kwargs['ephemeral'] is True
        finally:
            uninstall()

    asyncio.run(journey())


def test_long_queue_keeps_all_remove_ids_within_message_embed_budget(tmp_path):
    service, _, message, _ = fixture(tmp_path)
    message.channel = TextChannel()

    async def journey():
        install(service.bot, service)
        try:
            await service.execute(message, 'play', query='current')
            session = service.sessions[10]
            session.queue = [
                Track('encoded', f'Song {i} ' + 'long title ' * 20, 'artist ' * 20,
                      f'https://www.youtube.com/watch?v={i:011d}', 120000, requester=100)
                for i in range(25)]
            request = interaction(message, 'queue')
            await music_handler()(service.bot, request)
            fields = []
            for call in request.followup.send.call_args_list:
                embeds = call.kwargs['embeds']
                assert sum(len(embed) for embed in embeds) <= 6000
                fields.extend(field.value for embed in embeds for field in embed.fields)
            assert len(fields) == 25
            for queued, field in zip(session.queue, fields, strict=True):
                assert queued.key in field
                assert queued.uri in field
        finally:
            uninstall()

    asyncio.run(journey())


def test_embed_bounds_untrusted_text_and_explains_autoplay_origin():
    track = Track('encoded', '@everyone **title** ' * 300, '@here [artist] ' * 300,
        'https://www.youtube.com/watch?v=abcdefghijk', 3700000,
        artwork='https://example.com/art.jpg', requester=100, autoplay=True)
    embed = state_embed({'connected': True, 'current': track.public(),
        'voice_channel': {'id': '11', 'name': '@everyone ' * 300}, 'paused': True,
        'queue': [track.public()] * 3, 'queue_length': 3, 'error': '@everyone **error** ' * 300,
        'volume': 80, 'repeat': 'queue', 'autoplay': True})
    assert 'Paused' in embed.title
    assert any('Autoplay · seeded by <@100>' in f.value for f in embed.fields)
    assert any('1:01:40' == f.value for f in embed.fields)
    assert '<@100>' not in embed.description
    assert '@everyone' not in embed.description
    assert embed.thumbnail.url == 'https://example.com/art.jpg'
    assert len(embed) <= 6000
    assert len(embed.title) <= 256 and len(embed.description) <= 4096
    assert all(len(f.name) <= 256 and len(f.value) <= 1024 for f in embed.fields)


def test_transient_edit_failure_does_not_spam_or_invalidate_controls(tmp_path):
    service, _, message, _ = fixture(tmp_path)
    message.channel = TextChannel()

    async def journey():
        service.panel_update = update_panel
        await service.execute(message, 'play', query='first')
        session = service.sessions[10]
        panel = session.panel
        edit = panel.edit.side_effect
        initial_toggle = panel.payload['view'].children[0].label
        panel.edit.side_effect = discord.HTTPException(
            SimpleNamespace(status=500, reason='Server Error'), 'Temporary')
        await service.execute(message, 'pause')
        assert session.paused and session.panel is panel
        assert message.channel.messages == [panel]
        panel.edit.side_effect = edit
        await update_panel(session)
        assert panel.payload['view'].children[0].label != initial_toggle
        assert message.channel.messages == [panel]

    asyncio.run(journey())
