"""Opt-in Last.fm account consent and listener-aware music scrobbling."""
from maxwell_core.prompts.component import PromptComponent


async def setup(bot, ctx):
    from .service import LastFmService
    from .scrobble import ScrobbleManager
    from .tools import LastFmAccountTool
    from .ui import install

    accounts = LastFmService(ctx.store_path('accounts.sqlite3'))
    manager = None
    try:
        await accounts.start()
        music = ctx.service('music')
        if music is None:
            raise RuntimeError('Last.fm requires the music plugin service')
        manager = ScrobbleManager(music, accounts, ctx.store_path('scrobbles.sqlite3'))
        await manager.start()
        accounts.scrobbler = manager
        ctx.register_service('lastfm', accounts)
        ctx.every(5, manager.tick)
        ctx.every(5, accounts.expire_pending)
        ctx.on_event('on_voice_state_update', manager.voice_state_changed)
        ctx.register_prompt(PromptComponent(
            id='lastfm.instructions', plugin='lastfm', position='tools',
            requires_tools=('lastfm_account',),
            text=('Last.fm is opt-in and private. Use lastfm_account only for the current requester '
                  'when they ask to connect, finish browser approval, check status or disconnect. '
                  'connect privately delivers the approval link; never request passwords, tokens or '
                  'session keys, or put an approval URL in public chat. If DMs cannot be delivered, '
                  'tell them to use /lastfm connect. Approval requires their browser then Finish '
                  'linking or /lastfm finish; do not poll or claim linking before confirmed success. '
                  'Only eligible actual listening in Maxwell\'s voice channel counts, not requests '
                  'from elsewhere. Never infer a user\'s Last.fm identity or listening history.')))
        install(bot, accounts)
    except BaseException:
        try:
            if manager is not None:
                await manager.close()
        finally:
            await accounts.close()
        raise
    return [LastFmAccountTool(bot, accounts)]


async def teardown(bot, ctx):
    from .ui import uninstall
    uninstall()
    accounts = ctx.service('lastfm')
    if accounts is not None:
        try:
            await accounts.quiesce()
            if accounts.scrobbler is not None:
                await accounts.scrobbler.close()
        finally:
            await accounts.close()
