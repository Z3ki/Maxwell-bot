"""Private browser approval and explicit, owner-bound redemption; no callback server."""
import discord
import user_install as ui

from .client import LastFmError
from .tools import account_status, requester

PREFIX = 'maxwell_lastfm:'


def command_definition():
    return {'name': 'lastfm', 'description': 'Manage your private Last.fm connection', 'type': 1,
            'integration_types': [0, 1], 'contexts': [0, 1, 2], 'options': [
                {'type': 1, 'name': 'connect', 'description': 'Get a private Last.fm browser approval link'},
                {'type': 1, 'name': 'finish', 'description': 'Finish linking after approving Maxwell in your browser'},
                {'type': 1, 'name': 'status', 'description': 'Privately check your Last.fm connection'},
                {'type': 1, 'name': 'disconnect', 'description': 'Remove your connection and pending listening'}]}


def approval_text():
    return ('Use **Approve on Last.fm** to approve Maxwell in your browser. '
            'Then click **Finish linking**, or use **/lastfm finish** in the server. '
            'This approval expires in 60 minutes. Do not share the link. '
            'No password is sent to Maxwell. Linking opts you into scrobbling eligible music '
            'you actually listen to in Maxwell\'s voice channel. '
            'Use /lastfm disconnect to stop and remove pending uploads.')


class FinishView(discord.ui.View):
    def __init__(self, owner, nonce, url):
        super().__init__(timeout=3600)
        self.add_item(discord.ui.Button(label='Approve on Last.fm', style=discord.ButtonStyle.link,
                                       url=url))
        self.add_item(discord.ui.Button(label='Finish linking', style=discord.ButtonStyle.primary,
                                       custom_id=f'{PREFIX}{owner}:{nonce}'))


def install(bot, service):
    ui.register_command(command_definition())

    async def handler(host, interaction):
        if host is not bot:
            return False
        data = interaction.data or {}
        custom = str(data.get('custom_id') or '')
        button = custom.startswith(PREFIX)
        if not button and data.get('name') != 'lastfm':
            return False
        await interaction.response.defer(ephemeral=True)
        try:
            user = requester(bot, interaction)
            owner = str(user.id)
            nonce = None
            if button:
                parts = custom[len(PREFIX):].split(':')
                if len(parts) != 2 or parts[0] != owner or not 1 <= len(parts[1]) <= 64:
                    raise LastFmError('This linking button belongs to another requester or is no longer valid.')
                action, nonce = 'finish', parts[1]
            else:
                options = data.get('options') or []
                if len(options) != 1 or options[0].get('options'):
                    raise LastFmError('Choose /lastfm connect, finish, status or disconnect.')
                action = options[0].get('name')
            view = None
            if action == 'connect':
                url, nonce = await service.connect(owner)
                text, view = approval_text(), FinishView(owner, nonce, url)
            elif action == 'finish':
                await service.finish(owner, nonce)
                text = 'Last.fm is linked. Eligible music you listen to in Maxwell\'s voice channel will be scrobbled. Use /lastfm disconnect to stop.'
            elif action == 'disconnect':
                await service.disconnect(owner)
                text = 'Your Last.fm connection, pending approvals and queued listening have been removed.'
            elif action == 'status':
                status = await account_status(service, owner)
                text = ('Last.fm is linked.' if status['linked'] else 'Last.fm is not linked. Use /lastfm connect.')
                text += f" Pending scrobbles: {status['pending_scrobbles']}."
            else:
                raise LastFmError('Choose /lastfm connect, finish, status or disconnect.')
        except LastFmError as exc:
            text, view = str(exc), None
        except Exception:
            text, view = 'The Last.fm operation could not be confirmed. Use /lastfm status or contact the operator.', None
        kwargs = {'ephemeral': True, 'allowed_mentions': discord.AllowedMentions.none()}
        if view is not None:
            kwargs['view'] = view
        await interaction.followup.send(text, **kwargs)
        return True

    ui.register_interaction_handler(handler, priority=5, name='lastfm')


def uninstall():
    ui.unregister_command('lastfm')
    ui.unregister_interaction_handler('lastfm')
