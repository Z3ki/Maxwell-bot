"""Persistent autonomous life + per-user YOLO sandbox."""


def setup(bot, ctx):
    from .impl import AgentLifeService, AgentLifeTool, UserSandboxTool

    service = AgentLifeService(bot, ctx)
    ctx.register_service("agent_life", service)
    ctx.every(15.0, service.tick, run_immediately=False)
    return [AgentLifeTool(bot, service), UserSandboxTool(bot, service)]
