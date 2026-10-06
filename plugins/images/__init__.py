"""Register Image Generation tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('image_generator', 'ImageGeneratorTool', 'ENABLE_IMAGE_GEN'),
        ('hd_image', 'HDImageGeneratorTool', ('ENABLE_IMAGE_GEN', 'ENABLE_HD_IMAGE')),
    ])
