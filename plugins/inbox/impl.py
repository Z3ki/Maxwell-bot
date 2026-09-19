"""Tool implementations for the inbox plugin.

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

class InboxListTool(Tool):
    """List unread inbox items (friend requests, notices)."""
    tool_name = 'inbox_list'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List unread inbox items: friend requests, new email, and other "
            "notices. No params. Use inbox_act to accept, decline, dismiss, or "
            "mark read. For an email item, email_get_message with the item's "
            "uid gives you the full body."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        store = getattr(self.bot, "inbox", None)
        if store is None:
            return "Error: inbox is not available"
        items = store.actionable(await store.load_items())
        if not items:
            return "Inbox is empty."
        # Same ordering the planner tail uses, but the tool shows more of each
        # item — he asked for the list, so give him the whole thing.
        ordered = store.planner_items(items)
        lines = [f"Inbox ({len(items)} actionable):"]
        lines.extend(
            store.render_item(item, summary_chars=300) for item in ordered[:20]
        )
        if len(items) > len(ordered):
            lines.append(f"… {len(items) - len(ordered)} more not shown")
        return "\n".join(lines)

class InboxActTool(Tool):
    """Accept, decline, or dismiss an inbox item."""
    tool_name = 'inbox_act'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Act on an inbox item. Params: action (required: accept, decline, "
            "dismiss, or read), item_id (inbox id like friend_123 or "
            "email_412) or user_id (the requester's Discord id). accept and "
            "decline are friend requests only; read keeps a notice in the "
            "inbox but stops it being brought to your attention again, "
            "dismiss clears it for good."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        item_id: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        from inbox import apply_inbox_action

        return await apply_inbox_action(
            self.bot,
            action=str(action or ""),
            item_id=str(item_id or ""),
            user_id=re.sub(r"[^0-9]", "", str(user_id or "")),
        )
