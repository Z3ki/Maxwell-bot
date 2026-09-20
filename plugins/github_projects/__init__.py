"""GitHub project automation plugin."""
from __future__ import annotations

def setup(bot, ctx=None):
    from .impl import GitHubRepoTool, GitHubProjectService
    service = GitHubProjectService(bot, ctx)
    if ctx is not None:
        ctx.register_service("github_projects", service)
        ctx.every(90, service.poll_once, run_immediately=False)
    tool = GitHubRepoTool(bot, service)
    tool.name = "github_repo"
    tool.tool_name = "github_repo"
    return [tool]
