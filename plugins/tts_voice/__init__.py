"""Register Mistral text-to-speech with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(
        bot,
        __package__,
        [
            ("tts", "TtsTool", None),
        ],
    )
