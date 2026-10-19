"""GitHub project automation plugin."""
from __future__ import annotations

def setup(bot, ctx=None):
    from .impl import GitHubRepoTool, GitHubProjectService
    service = GitHubProjectService(bot, ctx)
    if ctx is not None:
        ctx.register_service("github_projects", service)
    tool = GitHubRepoTool(bot, service)
    tool.name = "github_repo"
    tool.tool_name = "github_repo"
    return [tool]


async def teardown(bot, ctx=None):
    """Close the service's HTTP session so a reload does not leak a connector."""
    service = ctx.service("github_projects") if ctx is not None else None
    close = getattr(service, "close", None)
    if callable(close):
        await close()
