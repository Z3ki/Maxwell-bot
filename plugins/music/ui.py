"""Guild-only slash commands and one reusable Now Playing panel."""
from __future__ import annotations

import json

import discord
import user_install as ui

from .tools import perform


def option(name, description, kind=3, *, required=False, choices=None, **extra):
    data = {'type': kind, 'name': name, 'description': description, 'required': required, **extra}
    if choices:
        data['choices'] = [{'name': c, 'value': c} for c in choices]
    return data


def command_definition():
    def sub(name, description, options=()):
        return {'type': 1, 'name': name, 'description': description, 'options': list(options)}
    return {'name': 'music', 'description': 'Play music with Maxwell', 'type': 1,
            'integration_types': [0], 'contexts': [0], 'dm_permission': False, 'options': [
                sub('play', 'Play a song or queue it behind the current song', [
                    option('query', 'Song name or YouTube video/playlist URL', required=True, max_length=400),
                    option('mode', 'Queue, play next, or replace now', choices=['queue', 'next', 'now']),
                    option('channel', 'Voice channel (defaults to yours)', 7, channel_types=[2])]),
                sub('join', 'Join your voice channel', [option('channel', 'Voice channel', 7, channel_types=[2])]),
                sub('move', 'Move the music session (DJ or manager)', [option('channel', 'Voice channel', 7, required=True, channel_types=[2])]),
                sub('search', 'Find songs without playing', [option('query', 'Song name', required=True, max_length=400)]),
                sub('status', 'Show current song and voice channel'),
                sub('queue', 'Show upcoming songs', [option('offset', 'Skip this many queue entries', 4, min_value=0, max_value=1000)]),
                sub('pause', 'Pause playback'), sub('resume', 'Resume playback'), sub('skip', 'Skip the current track'),
                sub('stop', 'Stop and clear queue (DJ or manager)'), sub('leave', 'Disconnect (DJ or manager)'),
                sub('volume', 'Set volume from 0 to 100', [option('value', 'Volume percentage', 4, required=True, min_value=0, max_value=100)]),
                sub('seek', 'Seek to a time in the current song', [option('value', 'Seconds from start', 4, required=True, min_value=0)]),
                sub('repeat', 'Set repeat mode', [option('value', 'Repeat mode', required=True, choices=['off', 'track', 'queue'])]),
                sub('shuffle', 'Shuffle upcoming songs'), sub('clear', 'Clear queue (DJ or manager)'),
                sub('remove', 'Remove one of your queued songs', [option('track_id', 'Track ID from /music queue', required=True)]),
                sub('configure', 'View or edit server music settings (server manager)', [
                    option('default_channel', 'Fallback voice channel', 7, channel_types=[2]),
                    option('dj_role', 'Role permitted to manage playback', 8),
                    option('listener_controls', 'Allow listeners to pause, skip, seek, shuffle and repeat', 5),
                    option('idle_seconds', 'Leave after idle or empty channel time', 4, min_value=30, max_value=3600),
                    option('queue_limit', 'Maximum queued songs', 4, min_value=1, max_value=100),
                    option('user_queue_limit', 'Maximum queued songs per listener', 4, min_value=1, max_value=100),
                    option('reset_default', 'Remove fallback voice channel', 5),
                    option('reset_dj', 'Remove DJ role', 5)])]}


def safe(text):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text)))


def format_result(result):
    if result.get('error') and 'connected' not in result:
        return result['error']
    if 'results' in result:
        return '\n'.join(f"{i + 1}. {safe(t['title'])} — {safe(t['artist'])}\n{t['url']}" for i, t in enumerate(result['results']))[:1900]
    if 'settings' in result:
        return 'Music settings' + (' saved' if result.get('saved') else '') + ':\n```json\n' + json.dumps(result['settings'], indent=2) + '\n```'
    if 'connected' in result:
        channel = result.get('voice_channel')
        current = result.get('current')
        if not result['connected']:
            status = 'Disconnected from voice. Nothing is playing.'
        elif current:
            state = 'Paused' if result.get('paused') else 'Playing'
            seconds = max(0, int(result.get('position_seconds') or 0))
            status = f"{state} **{safe(current['title'])}** · {seconds // 60}:{seconds % 60:02d}."
        else:
            status = 'Connected; nothing is playing.'
        if channel:
            status += f"\nVoice channel: **{safe(channel['name'])}** (<#{channel['id']}>)."
        status += f"\nVolume {result.get('volume', 50)}% · Repeat {safe(result.get('repeat', 'off'))} · {result.get('queue_length', 0)} queued."
        if result.get('error'):
            status += '\n' + safe(result['error'])
        if result.get('audio_available') is False:
            status += '\nThe audio node is unavailable.'
        return status[:1900]
    if 'queue' in result and 'state' not in result:
        return ('\n'.join(f"{i + 1 + result.get('offset', 0)}. {safe(t['title'])} · requested by <@{t['requester_id']}>\nID: `{t['id']}`"
                          for i, t in enumerate(result['queue'])) or 'The queue is empty.')[:1900]
    if result.get('started'):
        return f"Playing **{safe(result['started']['title'])}**."
    if 'queued' in result:
        return f"Added {result['added']} song(s) to the queue."
    if result.get('joined'):
        return f"Joined <#{result['joined']['id']}>."
    if result.get('left'):
        return 'Disconnected from voice.'
    if result.get('stopped'):
        return 'Stopped playback and cleared the queue.'
    if 'state' in result:
        return 'Music updated.'
    current = result.get('current')
    return f"Playing **{safe(current['title'])}**." if current else 'Nothing is playing.'


def panel_view(gid):
    view = discord.ui.View(timeout=None)
    for action, label, style in [('toggle', 'Pause / Resume', discord.ButtonStyle.primary),
                                 ('skip', 'Skip', discord.ButtonStyle.secondary),
                                 ('queue', 'Queue', discord.ButtonStyle.secondary),
                                 ('stop', 'Stop', discord.ButtonStyle.danger)]:
        view.add_item(discord.ui.Button(label=label, style=style, custom_id=f'maxwell_music:{gid}:{action}'))
    return view


async def update_panel(session):
    current = session.current
    description = (f"**{safe(current.title)}**\n{safe(current.author)}\n"
                   f"Requested by <@{current.requester}> · {current.duration_ms // 60000}:{current.duration_ms // 1000 % 60:02d}") if current else 'Nothing is playing.'
    if session.error:
        description += '\n' + session.error
    embed = discord.Embed(title='Maxwell Music', description=description, color=0x7755DD)
    if current and current.artwork and current.artwork.startswith('https://'):
        embed.set_thumbnail(url=current.artwork)
    channel = f' · {session.channel.name}' if session.channel else ' · Disconnected'
    embed.set_footer(text=f"{'Paused' if session.paused else 'Playing' if current else 'Idle'} · Volume {session.volume}% · {len(session.queue)} queued · Repeat {session.repeat}{channel}"[:2000])
    view = panel_view(session.guild.id) if session.channel else None
    kwargs = {'embed': embed, 'view': view, 'allowed_mentions': discord.AllowedMentions.none()}
    if session.panel:
        try:
            await session.panel.edit(**kwargs)
            return
        except discord.NotFound:
            session.panel = None
    if session.text_channel and session.channel:
        session.panel = await session.text_channel.send(**kwargs)


def install(bot, service):
    ui.register_command(command_definition())
    service.panel_update = update_panel

    async def handler(host, interaction):
        if host is not bot:
            return False
        data = interaction.data or {}
        custom = str(data.get('custom_id') or '')
        button = custom.startswith('maxwell_music:')
        if not button and data.get('name') != 'music':
            return False
        # Acknowledge before networking, permission checks or waiting for a lock.
        await interaction.response.defer(ephemeral=True)
        if button:
            pieces = custom.split(':')
            guild = interaction.guild
            session = service.sessions.get(guild.id) if guild else None
            if (len(pieces) != 3 or not guild or pieces[1] != str(guild.id) or not session
                    or not session.panel or session.panel.id != getattr(interaction.message, 'id', None)):
                result = {'error': 'This music panel is no longer active. Use /music status.'}
            else:
                action = pieces[2]
                if action == 'toggle':
                    action = 'resume' if session.paused else 'pause'
                if action not in {'resume', 'pause', 'skip', 'queue', 'stop'}:
                    result = {'error': 'Unknown music button.'}
                else:
                    result = await perform(service, interaction, action)
        else:
            sub = (data.get('options') or [{}])[0]
            action = sub.get('name', '')
            args = {o['name']: o.get('value') for o in sub.get('options', [])}
            if action == 'configure':
                if args.pop('reset_default', False):
                    args['default_channel'] = None
                if args.pop('reset_dj', False):
                    args['dj_role'] = None
                args = {'settings': args or None}
            result = await perform(service, interaction, action, **args)
        await interaction.followup.send(format_result(result), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())
        return True

    ui.register_interaction_handler(handler, priority=5, name='music')


def uninstall():
    ui.unregister_command('music')
    ui.unregister_interaction_handler('music')
