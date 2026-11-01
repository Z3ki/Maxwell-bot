"""Music tools and playback transitions do not publish Discord UI."""
import asyncio
import json

from plugins.music.tools import MusicControlTool, MusicInfoTool
from test_music import fixture


def test_tool_actions_return_state_without_posting(tmp_path):
    service, backend, message, _ = fixture(tmp_path)
    message.author.guild_permissions.manage_guild = True
    control = MusicControlTool(service.bot, service)
    info = MusicInfoTool(service.bot, service)

    async def action(name, **kwargs):
        return json.loads(await control.execute(message, action=name, **kwargs))

    async def journey():
        result = await action('play', query='first')
        assert result['started']['title'] == 'first'
        current = result['state']['current']
        assert (current['artist'], current['duration_seconds'], current['requester_id']) == ('artist', 120, '100')
        assert result['state']['position_seconds'] == 12
        assert (await action('play', query='second'))['queued'][0]['title'] == 'second'
        assert (await action('pause'))['paused'] is True
        assert (await action('resume'))['paused'] is False
        assert (await action('volume', value=65))['state']['volume'] == 65
        queue = json.loads(await info.execute(message, action='queue'))
        assert queue['total'] == 1 and queue['queue'][0]['title'] == 'second'
        assert (await action('skip'))['current']['title'] == 'second'
        assert (await action('stop'))['stopped'] is True
        status = json.loads(await info.execute(message, action='status'))
        assert status['current'] is None and status['queue'] == []
        message.channel.send.assert_not_awaited()
        assert ('pause', 10, True) in backend.calls
        assert ('pause', 10, False) in backend.calls
        assert ('volume', 10, 65) in backend.calls

    asyncio.run(journey())


def test_track_end_and_queue_changes_are_silent_and_notify_observer(tmp_path):
    service, _, message, _ = fixture(tmp_path)
    events = []
    service.playback_observer = lambda session, event: events.append(
        (event, session.current.title if session.current else None))

    async def journey():
        await service.execute(message, 'play', query='first')
        await service.execute(message, 'play', query='second')
        session = service.sessions[10]
        await service.ended(10, session.current.key)
        assert session.current.title == 'second' and not session.queue
        await service.ended(10, session.current.key)
        assert session.current is None
        assert ('start', 'first') in events and ('end', 'first') in events
        assert ('start', 'second') in events and ('end', 'second') in events
        message.channel.send.assert_not_awaited()

    asyncio.run(journey())


def test_status_preview_is_compact_and_queue_is_paginated(tmp_path):
    service, _, message, _ = fixture(tmp_path)
    message.author.guild_permissions.manage_guild = True
    control = MusicControlTool(service.bot, service)
    info = MusicInfoTool(service.bot, service)

    async def journey():
        await control.execute(message, action='play', query='current')
        await control.execute(message, action='play', queries=[f'song {n}' for n in range(10)])
        await control.execute(message, action='play', queries=['song 10', 'song 11'])
        status = json.loads(await info.execute(message, action='status'))
        assert status['queue_length'] == 12 and len(status['queue']) == 10
        assert status['queue_remaining'] == 2 and status['queue_note'] == '+ 2 more'
        assert status['queue'][0]['title'] == 'song 0'
        first_page = json.loads(await info.execute(message, action='queue'))
        assert len(first_page['queue']) == 10
        assert first_page['queue_remaining'] == 2 and first_page['queue_note'] == '+ 2 more'
        page = json.loads(await info.execute(message, action='queue', offset=10))
        assert page['total'] == 12 and [t['title'] for t in page['queue']] == ['song 10', 'song 11']
        assert page['queue_remaining'] == 0 and page['queue_note'] is None
        message.channel.send.assert_not_awaited()

    asyncio.run(journey())
