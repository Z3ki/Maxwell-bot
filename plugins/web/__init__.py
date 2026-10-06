"""Register Web tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('web_search', 'WebSearchTool', 'ENABLE_WEB_SEARCH'),
        ('fetch_url', 'FetchUrlTool', 'ENABLE_FETCH_URL'),
    ])



async def teardown(bot):
    tool = (getattr(bot, "tools", None) or {}).get("web_search")
    close = getattr(tool, "close", None)
    if callable(close):
        await close()
