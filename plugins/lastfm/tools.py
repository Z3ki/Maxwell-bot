"""Account controls expose no credentials, approval URL, or account identity to AI."""
import json

import discord

from tools import Tool
from .client import LastFmError

ACTIONS = ('connect', 'finish', 'status', 'disconnect')


def requester(bot, context):
    guard = getattr(bot, '_dev_scope_allows', None)
    if callable(guard):
        allowed = guard(context)
    else:
        config = getattr(bot, 'config', None)
        allowed = not getattr(config, 'MAXWELL_DEV_MODE', False)
    if not allowed:
        raise LastFmError('Last.fm is unavailable outside the configured development server.')
    user = getattr(context, 'author', None) or getattr(context, 'user', None)
    if user is None or getattr(user, 'bot', False):
        raise LastFmError('An authenticated Discord requester is required.')
    owner = str(getattr(user, 'id', '') or '')
    if not owner.isascii() or not owner.isdigit() or int(owner) <= 0:
        raise LastFmError('An authenticated Discord requester is required.')
    guild = getattr(context, 'guild', None)
    guild_id = str(guild.id) if guild else None
    manager = getattr(bot, 'plugin_manager', None)
    if manager is not None and not manager.is_plugin_enabled_for_user('lastfm', owner, guild_id=guild_id):
        raise LastFmError('Last.fm is disabled for this request. Check /config.')
    control = getattr(bot, '_control', {}) or {}
    if guild_id and 'plugins' in control.get('guild_disabled_capabilities', {}).get(guild_id, []):
        raise LastFmError('Optional plugins are disabled in this server.')
    return user


async def account_status(service, owner):
    service._require()
    account = service.account_for(owner)
    return {'succeeded': True, 'linked': account is not None,
            'pending_scrobbles': await service.scrobbler.pending_count(owner) if service.scrobbler is not None else 0}


class LastFmAccountTool(Tool):
    tool_name = 'lastfm_account'
    returns_result = True
    is_destructive = False
    side_effects = True
    transports = ('discord', 'user_install')
    timeout_seconds = 60

    def __init__(self, bot, service):
        super().__init__(bot)
        self.service = service

    def get_parameters(self):
        return {'type': 'object', 'additionalProperties': False, 'properties': {
            'action': {'type': 'string', 'enum': list(ACTIONS)}}, 'required': ['action']}

    def get_description(self):
        return ('Manage only the current requester\'s opt-in Last.fm connection. connect sends a private DM '
                'with browser approval and a Finish linking button; never asks for a password. '
                'After browser approval use finish; status reads connection and pending upload count; '
                'disconnect removes consent and queued listening. No user/account overrides. '
                'Approval links and credentials are never returned to chat or model context. '
                'If DMs are unavailable, direct the user to /lastfm connect. '
                'Link only when asked; no polling or claiming successful linking before finish confirms it.')

    async def execute(self, message, action, **kwargs):
        try:
            if action not in ACTIONS or kwargs:
                raise LastFmError('Choose connect, finish, status or disconnect; no account overrides are allowed.')
            user = requester(self.bot, message)
            owner = str(user.id)
            if action == 'connect':
                from .ui import FinishView, approval_text
                url, nonce = await self.service.connect(owner)
                try:
                    await user.send(approval_text(), view=FinishView(owner, nonce, url),
                                    allowed_mentions=discord.AllowedMentions.none())
                except (discord.HTTPException, AttributeError):
                    result = {'succeeded': False, 'error': 'Private DM could not be delivered. Use /lastfm connect in the server for a private approval link.'}
                else:
                    result = {'succeeded': True, 'private_approval_sent': True,
                              'message': 'Approve the application in your private DM, then click Finish linking or use /lastfm finish.'}
            elif action == 'finish':
                await self.service.finish(owner)
                result = {'succeeded': True, 'linked': True}
            elif action == 'disconnect':
                await self.service.disconnect(owner)
                result = {'succeeded': True, 'linked': False, 'message': 'Connection and pending listening were removed.'}
            else:
                result = await account_status(self.service, owner)
        except LastFmError as exc:
            result = {'succeeded': False, 'error': str(exc)}
        except Exception:
            result = {'succeeded': False, 'error': 'The Last.fm account operation could not be confirmed. Use /lastfm status or contact the operator.'}
        return json.dumps(result)
