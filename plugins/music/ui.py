"""Guild-only music commands and event-driven public Now Playing panels."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

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
                sub('batch', 'Add one to ten songs, one query or YouTube link per line', [
                    option('input', 'One song query or YouTube link per line (1–10 lines)', required=True, max_length=4000),
                    option('mode', 'Queue, play next, or replace now', choices=['queue', 'next', 'now'])]),
                sub('autoplay', 'Opt in to related songs when the queue ends', [
                    option('enabled', 'Automatically play related songs', 5, required=True)]),
                sub('join', 'Join your voice channel', [option('channel', 'Voice channel', 7, channel_types=[2])]),
                sub('move', 'Move the music session (DJ or manager)', [option('channel', 'Voice channel', 7, required=True, channel_types=[2])]),
                sub('search', 'Find songs without playing', [option('query', 'Song name', required=True, max_length=400)]),
                sub('status', 'Publish the current music panel here'),
                sub('queue', 'Show upcoming songs privately', [option('offset', 'Skip this many queue entries', 4, min_value=0, max_value=1000)]),
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
                    option('listener_controls', 'Allow listeners to control playback and autoplay', 5),
                    option('idle_seconds', 'Leave after idle or empty channel time', 4, min_value=30, max_value=3600),
                    option('queue_limit', 'Maximum queued songs', 4, min_value=1, max_value=100),
                    option('user_queue_limit', 'Maximum queued songs per listener', 4, min_value=1, max_value=100),
                    option('reset_default', 'Remove fallback voice channel', 5),
                    option('reset_dj', 'Remove DJ role', 5)])]}


def safe(text, limit=1000):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text)))[:limit]


def link_url(value):
    value = str(value or '')
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if len(value) <= 2048 and parsed.scheme == 'https' and parsed.netloc and not any(c in value for c in '\n\r<> ()') else None


def track_title(track, limit=180):
    title = safe(track.get('title') or 'Unknown song', limit)
    url = link_url(track.get('url'))
    return f'[{title}]({url})' if url and len(url) <= 400 else title


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}' if seconds >= 3600 else f'{seconds // 60}:{seconds % 60:02d}'


def state_embed(state):
    current = state.get('current')
    connected = state.get('connected', False)
    label = 'Paused' if current and state.get('paused') else 'Now Playing' if current else 'Ready to play' if connected else 'Disconnected'
    failed = state.get('error') or state.get('audio_available') is False
    embed = discord.Embed(title=f'Maxwell Music · {label}', color=0xED9B40 if failed or state.get('paused') else 0x7755DD)
    if current:
        artist = safe(current.get('artist') or 'Unknown artist', 160)
        url = link_url(current.get('url'))
        artist = f'[{artist}]({url})' if url and len(url) <= 400 else artist
        embed.description = f"{track_title(current)}\n{artist}"
        origin = 'Autoplay · seeded by' if current.get('autoplay') else 'Requested by'
        requester = str(current.get('requester_id') or '')
        who = f'<@{requester}>' if requester.isdigit() and int(requester) else 'unknown listener'
        embed.add_field(name='Duration', value=duration(current.get('duration_seconds')), inline=True)
        embed.add_field(name='Origin', value=f'{origin} {who}', inline=True)
        artwork = link_url(current.get('artwork'))
        if artwork:
            embed.set_thumbnail(url=artwork)
    else:
        embed.description = 'Queue a song with `/music play` or add several with `/music batch`.' if connected else 'Join a voice channel and use `/music play` to start listening.'
    channel = state.get('voice_channel')
    embed.add_field(name='Voice channel', value=safe(channel['name'], 160) if channel else 'Not connected', inline=True)
    embed.add_field(name='Playback', value=f"Volume **{state.get('volume', 50)}%** · Repeat **{safe(state.get('repeat', 'off'), 20)}** · Autoplay **{'on' if state.get('autoplay') else 'off'}**", inline=False)
    queue = state.get('queue') or []
    preview = '\n'.join(f'{i + 1}. {track_title(t, 100)}' for i, t in enumerate(queue[:3])) or 'The queue is empty.'
    embed.add_field(name=f"Up next · {state.get('queue_length', len(queue))} queued"[:256], value=preview[:1024], inline=False)
    if failed:
        embed.add_field(name='Playback notice', value=safe(state.get('error') or 'The audio node is unavailable.', 1024), inline=False)
    embed.set_footer(text='Controls require access to the active voice channel · /music queue for song IDs')
    return embed


def format_result(result):
    if result.get('error') and 'connected' not in result:
        return safe(result['error'], 1800)
    if 'settings' in result:
        return 'Music settings' + (' saved' if result.get('saved') else '') + ':\n```json\n' + json.dumps(result['settings'], indent=2)[:1500] + '\n```'
    if 'connected' in result:
        current = result.get('current')
        return f"{'Paused' if result.get('paused') else 'Playing'} **{safe(current['title'], 180)}**." if current else 'Connected; nothing is playing.' if result['connected'] else 'Disconnected from voice. Nothing is playing.'
    if result.get('started'):
        extra = f" Added {result['added']} more song(s)." if result.get('added') else ''
        return f"Playing **{safe(result['started']['title'], 180)}**.{extra}"
    if 'queued' in result:
        return f"Added {result.get('added', len(result['queued']))} song(s) to the queue."
    if result.get('joined'):
        return f"Joined **{safe(result['joined']['name'], 160)}**."
    if result.get('left'):
        return 'Disconnected from voice.'
    if result.get('stopped'):
        return 'Stopped playback, cleared the queue, and disabled autoplay.'
    if result.get('skipped'):
        return 'Skipped the current song.'
    if 'paused' in result:
        return 'Playback paused.' if result['paused'] else 'Playback resumed.'
    if 'autoplay' in result:
        return f"Autoplay {'enabled' if result['autoplay'] else 'disabled'}."
    if 'repeat' in result:
        return f"Repeat set to **{safe(result['repeat'], 20)}**."
    if 'volume' in result:
        return f"Volume set to {result['volume']}%."
    if 'position_seconds' in result:
        return f"Seeked to {duration(result['position_seconds'])}."
    if result.get('removed'):
        return f"Removed **{safe(result['removed']['title'], 180)}** from the queue."
    if result.get('shuffled'):
        return 'Shuffled the upcoming queue.'
    if result.get('queue_cleared'):
        return 'Cleared the queue.'
    return 'Music updated.'


def private_embeds(result):
    if 'queue' in result and 'connected' not in result and 'state' not in result or 'results' in result:
        searching = 'results' in result
        tracks = result.get('results' if searching else 'queue') or []
        offset = result.get('offset', 0)
        embeds = []
        for start in range(0, max(1, len(tracks)), 5):
            embed = discord.Embed(title='Song search' if searching else 'Upcoming queue', color=0x7755DD)
            for i, track in enumerate(tracks[start:start + 5], start + offset + 1):
                details = f"{track_title(track, 130)}\n{safe(track.get('artist', ''), 100)} · {duration(track.get('duration_seconds'))}"
                if not searching:
                    details += f"\nRemove with `/music remove track_id:{safe(track.get('id', ''), 80)}`"
                embed.add_field(name=f'{i}.', value=details[:1024], inline=False)
            if not tracks:
                embed.description = 'No songs found.' if searching else 'The queue is empty.'
            if not searching:
                end = offset + len(tracks)
                total = result.get('total', end)
                embed.set_footer(text=f'Showing {offset + 1}–{end} of {total} · /music queue offset:{end} for more' if tracks else f'{total} queued')
            embeds.append(embed)
        return embeds
    return [discord.Embed(title='Music · Request failed' if result.get('error') else 'Music', description=format_result(result), color=0xD9534F if result.get('error') else 0x7755DD)]


def panel_view(gid, state=None):
    state = state or {}
    view = discord.ui.View(timeout=None)
    controls = [('toggle', 'Resume' if state.get('paused') else 'Pause', discord.ButtonStyle.primary, not state.get('current')),
                ('skip', 'Skip', discord.ButtonStyle.secondary, not state.get('current')),
                ('queue', 'Queue', discord.ButtonStyle.secondary, False),
                ('shuffle', 'Shuffle', discord.ButtonStyle.secondary, state.get('queue_length', 0) < 2),
                ('repeat', f"Repeat: {state.get('repeat', 'off')}", discord.ButtonStyle.secondary, False),
                ('autoplay', f"Autoplay: {'on' if state.get('autoplay') else 'off'}", discord.ButtonStyle.success if state.get('autoplay') else discord.ButtonStyle.secondary, False),
                ('stop', 'Stop', discord.ButtonStyle.danger, False)]
    for i, (action, label, style, disabled) in enumerate(controls):
        view.add_item(discord.ui.Button(label=label, style=style, disabled=disabled, row=i // 4, custom_id=f'maxwell_music:{gid}:{action}'))
    return view


def session_state(session):
    return {'connected': bool(session.channel), 'current': session.current.public() if session.current else None,
            'voice_channel': {'id': str(session.channel.id), 'name': session.channel.name} if session.channel else None,
            'paused': session.paused, 'volume': session.volume, 'repeat': session.repeat,
            'autoplay': session.autoplay, 'queue': [t.public() for t in session.queue[:3]],
            'queue_length': len(session.queue), 'error': session.error}


async def update_panel(session, *, force=False):
    state = session_state(session)
    embed = state_embed(state)
    fingerprint = (json.dumps(embed.to_dict(), sort_keys=True),
                   getattr(session.text_channel, 'id', id(session.text_channel)),
                   getattr(session.channel, 'id', None),
                   session.current.key if session.current else None,
                   tuple(t.key for t in session.queue))
    if not force and session.panel and getattr(session, '_panel_fingerprint', None) == fingerprint:
        return True
    old = session.panel
    if not session.text_channel:
        return False
    view = panel_view(session.guild.id, state) if session.channel else None
    if old and getattr(getattr(old, 'channel', None), 'id', None) == session.text_channel.id:
        try:
            await old.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.Forbidden):
            # Deleted/uneditable panels need a replacement; reject their controls first.
            session.panel = None
        except discord.HTTPException:
            # A transient edit failure must not create another public message.
            return False
        else:
            session._panel_fingerprint = fingerprint
            return True
    else:
        session.panel = None
    if old:
        try:
            await old.edit(view=None)
        except discord.HTTPException:
            pass
    try:
        session.panel = await session.text_channel.send(embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        return False
    session._panel_fingerprint = fingerprint
    return True


def public_allowed(interaction):
    channel = interaction.channel
    permissions_for = getattr(channel, 'permissions_for', None)
    if not callable(permissions_for):
        return True
    perms = permissions_for(interaction.guild.me)
    send = getattr(perms, 'send_messages_in_threads', False) if isinstance(channel, discord.Thread) else perms.send_messages
    return bool(perms.view_channel and send and perms.embed_links)


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
        await interaction.response.defer(ephemeral=True)
        action, args = '', {}
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
                elif action == 'repeat':
                    args['value'] = {'off': 'track', 'track': 'queue', 'queue': 'off'}[session.repeat]
                elif action == 'autoplay':
                    args['value'] = not session.autoplay
                if action not in {'resume', 'pause', 'skip', 'queue', 'stop', 'shuffle', 'repeat', 'autoplay'}:
                    result = {'error': 'Unknown music button.'}
                else:
                    result = await perform(service, interaction, action, **args)
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
            elif action == 'autoplay':
                args['value'] = args.pop('enabled', None)
            elif action == 'batch':
                action = 'play'
                args['queries'] = [line.strip() for line in str(args.pop('input', '')).splitlines() if line.strip()]
            result = await perform(service, interaction, action, **args)
        if action == 'status' and 'connected' in result and interaction.guild and public_allowed(interaction):
            session = service.sessions.get(interaction.guild.id)
            published = False
            if session:
                async with session.lock:
                    session.text_channel = interaction.channel
                    published = await update_panel(session, force=True)
            else:
                try:
                    await interaction.channel.send(embed=state_embed(result), allowed_mentions=discord.AllowedMentions.none())
                    published = True
                except discord.HTTPException:
                    pass
            if published:
                await interaction.followup.send('Music status is shown here.', ephemeral=True)
                return True
        embeds = [state_embed(result)] if action == 'status' and 'connected' in result else private_embeds(result)
        batch, size = [], 0
        for embed in embeds:
            # Discord's 6,000-character limit covers ALL embeds in a message.
            if batch and size + len(embed) > 6000:
                await interaction.followup.send(embeds=batch, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                batch, size = [], 0
            batch.append(embed)
            size += len(embed)
        await interaction.followup.send(embeds=batch, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return True

    ui.register_interaction_handler(handler, priority=5, name='music')


def uninstall():
    ui.unregister_command('music')
    ui.unregister_interaction_handler('music')
