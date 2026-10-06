"""Register Shell Sandbox tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('shell', 'ShellTool', 'ENABLE_SHELL'),
    ])



async def teardown(bot):
    from .impl import ShellTool

    await ShellTool.shutdown_sandbox()
