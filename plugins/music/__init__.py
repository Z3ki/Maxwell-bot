"""Native AI DJ plugin, manual controls, and managed audio-node lifecycle."""
import os

from maxwell_core.prompts.component import PromptComponent


async def setup(bot, ctx):
    from .backend import LavalinkBackend
    from .service import MusicService
    from .tools import MusicControlTool, MusicInfoTool
    from .ui import install

    service = MusicService(bot, LavalinkBackend(bot), ctx.store_path('settings.json'))
    ctx.register_service('music', service)
    ctx.register_prompt(PromptComponent(
        id='music.instructions', plugin='music', position='tools',
        requires_tools=('music_control', 'music_info'),
        text=('You can act as a DJ using music_info and music_control in Discord servers. '
              'Choose songs naturally from the conversation; use search for titles and channels/status for live voice context. '
              'Play normally queues behind the current track; use now only for an explicit replacement, next for an explicit play-next request. '
              'Default voice selection follows the requester. If no usable voice channel exists, ask which channel to join. '
              'Only announce playback or changes when the tool confirms success. Do not claim every YouTube song is playable. '
              'Music has no microphone access. Retrieve live playback state on demand; never invent it.')))
    install(bot, service)

    async def ready():
        if os.getenv('LAVALINK_PASSWORD'):
            await service.backend.start()

    await service.load_settings()
    ctx.on_event('on_ready', ready)
    ctx.every(15, service.tick)
    return [MusicControlTool(bot, service), MusicInfoTool(bot, service)]


async def teardown(bot, ctx):
    from .ui import uninstall
    uninstall()
    service = ctx.service('music')
    if service:
        await service.close()
