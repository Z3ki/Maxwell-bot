"""Tool implementations for the plugin_admin plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

class ManagePluginTool(Tool):
    """Manage Maxwell modular plugins (enable, disable, list, status)."""
    tool_name = 'manage_plugin'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Manage modular plugins. Params: action (required: 'list', 'enable', 'disable', 'status'), "
            "plugin (optional, plugin name), user_id (optional, user ID or @mention), "
            "is_global (optional boolean, enable/disable plugin globally - requires admin)."
        )

    async def execute(
        self,
        message: Message,
        action: str = "list",
        plugin: str | None = None,
        user_id: str | None = None,
        is_global: bool = False,
        **kwargs,
    ) -> str:
        pm = getattr(self.bot, "plugin_manager", None)
        if not pm:
            return "Error: Plugin manager is not initialized on this bot."

        author_id = (
            message.author.id if message and hasattr(message, "author") else None
        )
        act = (action or "list").strip().lower()

        if act == "list" or act == "status":
            plugins = pm.list_plugins(user_id=author_id)
            if not plugins:
                return "No plugins currently installed in plugins/."
            lines = ["**Installed plugins:**"]
            for p in plugins:
                glob = "🌐 GLOBAL" if p["enabled_globally"] else "🔒 PER-USER"
                status = (
                    "✅ ACTIVE FOR YOU"
                    if p["enabled_for_you"]
                    else "❌ INACTIVE FOR YOU"
                )
                tools_str = ", ".join(p["tools"]) if p["tools"] else "none"
                lines.append(
                    f"• **{p['name']}** (v{p['version']}) — {glob} | {status}\n"
                    f"  {p['description']}\n"
                    f"  *Tools*: `{tools_str}`"
                )
            return "\n".join(lines)

        if not plugin:
            return f"Error: 'plugin' name is required for action '{act}'."

        plugin_name = plugin.strip().lower()

        is_global = parse_bool(is_global, False)
        author_id = str(author_id or "")
        is_admin = bool(
            author_id and getattr(self.bot, "_is_admin", lambda _uid: False)(author_id)
        )
        # Admin gate check for global modifications
        if is_global:
            if not is_admin:
                return "Error: Modifying global plugin status requires admin permissions."

        target_user = user_id
        if target_user:
            raw_target = str(target_user).strip()
            mention = re.fullmatch(r"<@!?(\d+)>", raw_target)
            if mention:
                target_user = mention.group(1)
            elif raw_target.isdigit():
                target_user = raw_target
            else:
                return "Error: user_id must be a numeric Discord user ID or mention."
            if not is_global and target_user != author_id and not is_admin:
                return (
                    "Error: you can only change plugin status for yourself; "
                    "admins may target another user."
                )
        elif not is_global and author_id:
            target_user = author_id

        if act == "enable":
            return pm.enable_plugin(
                plugin_name, user_id=target_user, is_global=is_global
            )
        elif act == "disable":
            return pm.disable_plugin(
                plugin_name, user_id=target_user, is_global=is_global
            )
        else:
            return f"Error: Unknown action '{act}'. Use 'list', 'enable', 'disable', or 'status'."
