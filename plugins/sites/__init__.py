"""Register Generated Sites tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('create_site', 'CreateSiteTool', 'ENABLE_CREATE_SITE'),
        ('edit_site', 'EditSiteTool', 'ENABLE_CREATE_SITE'),
        ('delete_site', 'DeleteSiteTool', 'ENABLE_CREATE_SITE'),
        ('site_server', 'SiteServerTool', 'ENABLE_CREATE_SITE'),
        ('list_sites', 'ListSitesTool', 'ENABLE_CREATE_SITE'),
        ('host_file', 'HostFileTool', 'ENABLE_CREATE_SITE'),
    ])
