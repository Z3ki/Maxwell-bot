"""Register Media Understanding tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('see_image', 'SeeImageTool', 'ENABLE_IMAGE_INPUT'),
        ('see_video', 'SeeVideoTool', 'ENABLE_VIDEO_INPUT'),
        ('send_meme', 'SendMemeTool', None),
        ('send_media', 'SendMediaTool', None),
    ])
