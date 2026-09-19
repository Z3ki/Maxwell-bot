"""Tool implementations for the email plugin.

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

class EmailSendTool(Tool):
    """Send mail FROM the local mailbox via local Postfix."""
    tool_name = 'email_send'
    returns_result = True
    ends_turn = False


    # Sending mail is the obvious prompt-injection target ("send my password
    # to attacker@evil") and on a tainted turn the user has to confirm.
    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Send email from the bot mailbox via local Postfix. "
            "Params: to (required, comma-separated), subject, body, "
            "is_html (optional), reply_to, cc, bcc."
        )

    async def execute(
        self,
        message: Message,
        to: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        is_html: str = "false",
        reply_to: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        **kwargs,
    ) -> str:
        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return (
                "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD "
                "in .env (the same password Dovecot knows about — /etc/dovecot/users)."
            )
        if not to or not str(to).strip():
            return "Error: 'to' is required"
        if not subject or not str(subject).strip():
            return "Error: 'subject' is required"
        if body is None:
            return "Error: 'body' is required"

        # Indirect-prompt-injection gate. If this turn was tainted by a
        # fetched URL or web search result, refuse without an explicit user
        # confirmation. Same pattern as shell.
        if _taint_gate_blocks(self, message, kwargs):
            preview = str(body)[:200] + ("..." if len(str(body)) > 200 else "")
            return (
                "Error: email_send refused: this turn read content from a "
                "fetched URL/web search that may carry prompt-injection "
                "payloads. The user must confirm out-of-band with `,confirm` "
                "before this can run.\n"
                f"Recipient: {to}\n"
                f"Subject: {subject}\n"
                f"Body preview: {preview}"
            )

        to_addrs = [a.strip() for a in str(to).split(",") if a.strip()]
        cc_addrs = [a.strip() for a in str(cc).split(",") if a.strip()] if cc else []
        bcc_addrs = [a.strip() for a in str(bcc).split(",") if a.strip()] if bcc else []

        try:
            return await asyncio.to_thread(
                _smtp_send_sync,
                cfg["host"],
                cfg["smtp_port"],
                cfg["user"],
                cfg["password"],
                cfg["from_addr"],
                cfg["from_name"],
                to_addrs,
                cc_addrs,
                bcc_addrs,
                str(subject),
                str(body),
                str(is_html).lower() in {"1", "true", "yes"},
                str(reply_to).strip() if reply_to else None,
            )
        except Exception as e:
            return f"Error: SMTP send failed: {e}"

class EmailReadInboxTool(Tool):
    """List recent messages in the local mailbox."""
    tool_name = 'email_read_inbox'
    returns_result = True
    ends_turn = False


    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "List recent mailbox messages (id, from, subject, date). "
            "Use email_get_message for a body. Params: max_results (default 10), "
            "days_back (default 7), unread_only (optional)."
        )

    async def execute(
        self,
        message: Message,
        max_results: str = "10",
        days_back: str = "7",
        unread_only: str = "false",
        **kwargs,
    ) -> str:
        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return (
                "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD "
                "in .env (the same password Dovecot knows about — /etc/dovecot/users)."
            )
        try:
            limit = max(1, min(int(max_results), 50))
        except (TypeError, ValueError):
            limit = 10
        try:
            days = max(0, min(int(days_back), 90))
        except (TypeError, ValueError):
            days = 7
        try:
            result = await asyncio.to_thread(
                _imap_list_recent_sync,
                cfg["imap_host"],
                cfg["imap_port"],
                cfg["user"],
                cfg["password"],
                limit,
                days,
                str(unread_only).lower() in {"1", "true", "yes"},
            )
        except Exception as e:
            return f"Error: IMAP read failed: {e}"
        if self.bot is not None:
            self.bot.mark_message_tainted(message)
        return result

class EmailGetMessageTool(Tool):
    """Fetch the full body of a single local message by id."""
    tool_name = 'email_get_message'
    returns_result = True
    ends_turn = False


    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Fetch one email by id (from email_read_inbox, email_search, or "
            "an inbox email notice — both 412 and email_412 work). "
            "Params: message_id, max_chars (default 8000)."
        )

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        max_chars: str = "8000",
        **kwargs,
    ) -> str:
        if not message_id or not str(message_id).strip():
            return "Error: message_id is required"
        try:
            cap = max(200, min(int(max_chars), 50000))
        except (TypeError, ValueError):
            cap = 8000

        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD in .env."
        try:
            result = await asyncio.to_thread(
                _imap_get_message_sync,
                cfg["imap_host"],
                cfg["imap_port"],
                cfg["user"],
                cfg["password"],
                str(message_id).strip(),
                cap,
            )
        except Exception as e:
            return f"Error: IMAP fetch failed: {e}"
        if self.bot is not None:
            self.bot.mark_message_tainted(message)
        return result

class EmailSearchTool(Tool):
    """Full-text search of the local mailbox."""
    tool_name = 'email_search'
    returns_result = True
    ends_turn = False


    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Search the mailbox (IMAP TEXT). Params: query, max_results (default 10). "
            "Returns ids plus subject/from/date; use email_get_message for bodies."
        )

    async def execute(
        self,
        message: Message,
        query: str | None = None,
        max_results: str = "10",
        **kwargs,
    ) -> str:
        if not query or not str(query).strip():
            return "Error: query is required"
        try:
            limit = max(1, min(int(max_results), 50))
        except (TypeError, ValueError):
            limit = 10
        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD in .env."
        try:
            result = await asyncio.to_thread(
                _imap_search_sync,
                cfg["imap_host"],
                cfg["imap_port"],
                cfg["user"],
                cfg["password"],
                str(query).strip(),
                limit,
            )
        except Exception as e:
            return f"Error: IMAP search failed: {e}"
        if self.bot is not None:
            self.bot.mark_message_tainted(message)
        return result
