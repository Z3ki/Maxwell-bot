"""Register Generated Sites tools with Maxwell."""
from __future__ import annotations

from typing import Any


def _flag(bot: Any, name: str | None) -> bool:
    if not name:
        return True
    cfg = getattr(bot, "config", None)
    if cfg is None:
        return True
    return bool(getattr(cfg, name, True))


def setup(bot, ctx=None):
    pass
    from .impl import CreateSiteTool, EditSiteTool, DeleteSiteTool, SiteServerTool, ListSitesTool, HostFileTool

    mapping = [
        ('create_site', CreateSiteTool, 'ENABLE_CREATE_SITE'),
        ('edit_site', EditSiteTool, 'ENABLE_CREATE_SITE'),
        ('delete_site', DeleteSiteTool, 'ENABLE_CREATE_SITE'),
        ('site_server', SiteServerTool, 'ENABLE_CREATE_SITE'),
        ('list_sites', ListSitesTool, 'ENABLE_CREATE_SITE'),
        ('host_file', HostFileTool, 'ENABLE_CREATE_SITE'),
    ]
    tools = []
    for runtime_name, cls, enable_name in mapping:
        if not _flag(bot, enable_name):
            continue
        inst = cls(bot)
        inst.name = runtime_name
        inst.tool_name = runtime_name
        tools.append(inst)
    return tools
