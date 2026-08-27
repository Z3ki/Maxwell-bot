"""OpenAI-compatible tool schemas for Maxwell native function calling.

Each entry maps a tool name to a JSON Schema ``parameters`` object. Descriptions
come from the live tool instances at request time so they stay in sync with
``get_description()``.
"""

from __future__ import annotations

import re
from typing import Any


def _obj(
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    additional: bool = True,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": additional,
    }
    if required:
        schema["required"] = required
    return schema


def _str(desc: str = "", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string", "description": desc}
    out.update(extra)
    return out


def _bool(desc: str = "") -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _int(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _num(desc: str = "") -> dict[str, Any]:
    return {"type": "number", "description": desc}


# parameter schemas only — descriptions are attached from tool.get_description()
TOOL_PARAMETERS: dict[str, dict[str, Any]] = {
    "image_generator": _obj(
        {"prompt": _str("Image generation prompt")},
        ["prompt"],
    ),
    "hd_image": _obj(
        {
            "prompt": _str(
                "What to generate, or — when an input image is supplied — the "
                "change to make to it (e.g. 'make the jacket red')"
            ),
            "image": _str(
                "Optional image to edit or use as reference: an http(s) URL "
                "(Discord CDN, a permanent URL from a previous image, any public "
                "link) or a local path Maxwell wrote. For several, pass a JSON "
                "list or a comma-separated string (max 4). Omit to generate from "
                "scratch; images attached to the user's message are used "
                "automatically."
            ),
        },
        ["prompt"],
    ),
    "react": _obj({"emoji": _str("Emoji or custom emoji name")}, ["emoji"]),
    "edit_message": _obj(
        {
            "message_id": _str("Message ID to edit"),
            "content": _str("New message content"),
        },
        ["message_id", "content"],
    ),
    "delete_message": _obj(
        {
            "message_id": _str("Message ID to delete"),
            "channel_id": _str("Optional channel ID if not the current channel"),
        },
        ["message_id"],
    ),
    "change_presence": _obj(
        {"status": _str("online | idle | dnd | invisible")},
        ["status"],
    ),
    "set_activity": _obj(
        {
            "text": _str("Activity or custom status text"),
            "type": _str("playing | watching | listening | competing | custom"),
            "elapsed": _str("Optional elapsed time (for custom status)"),
        },
        ["text"],
    ),
    "create_poll": _obj(
        {
            "question": _str("Poll question"),
            "options": _str("Comma-separated options"),
            "duration_hours": _num("Optional poll duration in hours"),
        },
        ["question", "options"],
    ),
    "create_invite": _obj(
        {
            "max_uses": _int("Max uses (default 1)"),
            "max_age": _int("Max age in seconds"),
        }
    ),
    "join_server": _obj(
        {
            "invite": _str(
                "The exact invite the user provided: a full URL "
                "(https://discord.gg/xyz, https://discord.com/invite/xyz) "
                "or the bare code. Never substitute a different invite."
            )
        },
        ["invite"],
    ),
    "server_setup": _obj(
        {
            "server": _str(
                "Server name or numeric ID. Omit to set up the current server."
            ),
            "preferences": _str(
                "Optional steer for which options to take, e.g. "
                "'only AI and coding stuff, no ping roles'"
            ),
            "list_only": _bool(
                "True to list the available roles/channels without picking any"
            ),
        }
    ),
    "leave_server": _obj(
        {"server": _str("Server name or numeric ID to leave")},
        ["server"],
    ),
    "lookup_user": _obj(
        {"user_id": _str("Numeric user ID or @mention")},
        ["user_id"],
    ),
    "search_messages": _obj(
        {
            "query": _str("Search query"),
            "limit": _int("Max results (default 5)"),
        },
        ["query"],
    ),
    "set_nickname": _obj(
        {"nickname": _str("New nickname, or 'reset' to clear")},
        ["nickname"],
    ),
    "forward_message": _obj(
        {
            "message_id": _str("Message ID to forward"),
            "channel_id": _str("Destination channel ID"),
        },
        ["message_id", "channel_id"],
    ),
    "typing": _obj({}),
    "list_servers": _obj({}),
    "list_admin_servers": _obj(
        {
            "guild_id": _str("Optional server ID for one-server detail"),
        }
    ),
    "create_category": _obj(
        {
            "name": _str("Category name"),
            "position": _int("Optional position"),
        },
        ["name"],
    ),
    "create_channel": _obj(
        {
            "name": _str("Channel name"),
            "type": _str("text or voice"),
            "kind": _str("Alias for type: text or voice"),
            "category_id": _str("Optional parent category ID"),
            "topic": _str("Optional channel topic"),
        },
        ["name"],
    ),
    "edit_channel": _obj(
        {
            "channel_id": _str("Channel ID"),
            "name": _str("New name"),
            "category_id": _str("New parent category ID"),
            "category_name": _str("New parent category name"),
            "topic": _str("New topic"),
            "slowmode_seconds": _int("Slowmode delay in seconds (0 to disable)"),
            "nsfw": _bool("Whether the channel is NSFW"),
            "position": _int("New position"),
        },
        ["channel_id"],
    ),
    "delete_channel": _obj(
        {
            "channel_id": _str("Channel or category ID"),
            "confirm_name": _str("Exact name confirmation"),
        },
        ["channel_id", "confirm_name"],
    ),
    "kick_member": _obj(
        {
            "user_id": _str("User ID or @mention to kick"),
            "reason": _str("Optional audit-log reason"),
            "guild_id": _str("Optional server ID"),
        },
        ["user_id"],
    ),
    "ban_member": _obj(
        {
            "user_id": _str("User ID or @mention to ban"),
            "reason": _str("Optional audit-log reason"),
            "delete_message_seconds": _str(
                "Optional 0-604800 seconds of messages to delete"
            ),
            "guild_id": _str("Optional server ID"),
        },
        ["user_id"],
    ),
    "unban_member": _obj(
        {
            "user_id": _str("User ID to unban"),
            "reason": _str("Optional audit-log reason"),
            "guild_id": _str("Optional server ID"),
        },
        ["user_id"],
    ),
    "list_bans": _obj(
        {
            "guild_id": _str("Optional server ID"),
            "limit": _int("Max bans to list (default 20)"),
        }
    ),
    "timeout_member": _obj(
        {
            "user_id": _str("User ID or @mention"),
            "duration": _str("e.g. 10m, 1h, 1d; 0/clear to remove"),
            "reason": _str("Optional audit-log reason"),
            "guild_id": _str("Optional server ID"),
        },
        ["user_id", "duration"],
    ),
    "manage_role": _obj(
        {
            "action": _str("list | create | edit | delete | add | remove"),
            "guild_id": _str("Optional server ID"),
            "name": _str("Role name (create/edit/lookup)"),
            "role_id": _str("Role ID"),
            "user_id": _str("Member for add/remove"),
            "color": _str("Optional hex color"),
            "hoist": _bool("Display separately"),
            "mentionable": _bool("Allow @role mentions"),
            "permissions": _str("Comma-separated Discord permission names"),
            "confirm_name": _str("Exact role name required to delete"),
        },
        ["action"],
    ),
    "purge_messages": _obj(
        {
            "limit": _int("How many recent messages to delete (1-100, default 20)"),
            "channel_id": _str("Optional channel ID"),
            "user_id": _str("Optional author filter"),
        }
    ),
    "pin_message": _obj(
        {
            "message_id": _str("Message ID to pin or unpin"),
            "channel_id": _str("Optional channel ID"),
            "unpin": _bool("True to unpin instead of pin"),
        },
        ["message_id"],
    ),
    "set_member_nickname": _obj(
        {
            "user_id": _str("Member to nick"),
            "nickname": _str("New nickname, or reset to clear"),
            "guild_id": _str("Optional server ID"),
        },
        ["user_id", "nickname"],
    ),
    "voice_mod": _obj(
        {
            "action": _str("mute | unmute | deafen | undeafen | move | disconnect"),
            "user_id": _str("Member in voice"),
            "channel_id": _str("Voice channel ID for move"),
            "guild_id": _str("Optional server ID"),
        },
        ["action", "user_id"],
    ),
    "lock_channel": _obj(
        {
            "channel_id": _str("Optional channel ID (defaults to current)"),
            "unlock": _bool("True to unlock"),
        }
    ),
    "set_channel_permissions": _obj(
        {
            "channel_id": _str("Channel ID"),
            "target": _str("Role ID, user ID, or everyone"),
            "allow": _str("Comma pairs like send_messages=false,view_channel=true"),
            "reset": _bool("True to clear that overwrite"),
        },
        ["channel_id", "target"],
    ),
    "edit_server": _obj(
        {
            "name": _str("New server name"),
            "description": _str("New server description"),
            "guild_id": _str("Optional server ID"),
        }
    ),
    "audit_log": _obj(
        {
            "guild_id": _str("Optional server ID"),
            "limit": _int("Entries to read (default 10)"),
        }
    ),
    "manage_emoji": _obj(
        {
            "action": _str("list | create | delete"),
            "name": _str("Emoji name"),
            "url": _str("Public image URL for create"),
            "emoji_id": _str("Emoji ID for delete"),
            "guild_id": _str("Optional server ID"),
        },
        ["action"],
    ),
    "change_avatar": _obj(
        {"url": _str("Direct image URL (jpg/png/gif/webp)")},
        ["url"],
    ),
    "create_site": _obj(
        {
            "name": _str("Short slug: lowercase, numbers, hyphens"),
            "title": _str(
                "Site title for listing/metadata — not a required on-page heading"
            ),
            "body": _str(
                "FULL HTML document (DOCTYPE through closing tags) for index.html. "
                "Served as-is: no restyle or layout template. Invent a new look each "
                "time unless the user specified one. Prefer this over stuffing HTML "
                "into chat. In visible HTML text use real line breaks or <br>, never "
                "literal \\n; keep \\n only inside intentional JavaScript/CSS strings."
            ),
            "files": _str(
                'Optional extra files as JSON: {"style.css": "...", "app.js": "...", '
                '"about/index.html": "..."}. Anything a static host serves — split a '
                "big page up, add subpages, ship a data.json. Paths are relative to "
                "the site root."
            ),
            "backend": _bool(
                "Give the site a live server-side store (named values + append-only "
                "lists) at /api/site/<name>/, same origin, no key needed. Turn it on "
                "for guestbooks, counters, polls, leaderboards, saved state, form "
                "submissions. The exact endpoints come back in the tool result."
            ),
            "permanent": _bool(
                "Skip the auto-expiry clock so the site stays up until deleted"
            ),
            "encoding": _str("text (default) or base64 for exact bytes"),
            "images": _str("Optional JSON list of local image paths to include"),
        },
        ["name", "title", "body"],
    ),
    "edit_site": _obj(
        {
            "name": _str("Slug of the site to edit (see list_sites)"),
            "action": _str(
                "list | read | write | replace | delete | rename | backend | extend"
            ),
            "path": _str("File inside the site, default index.html"),
            "content": _str("New file contents for write"),
            "find": _str("For replace: exact existing text to swap out"),
            "replace": _str("For replace: what to put there (empty string deletes it)"),
            "title": _str("For rename: the new title"),
            "encoding": _str("text (default) or base64 for write"),
            "backend": _str("For backend: true | false | status | clear"),
            "permanent": _bool("For extend: stop this site expiring"),
        },
        ["name", "action"],
    ),
    "site_server": _obj(
        {
            "name": _str("Slug of the site this backend belongs to"),
            "action": _str(
                "write | start | stop | restart | status | logs | read | env | delete"
            ),
            "files": _str(
                'Server source as JSON: {"app.py": "...", "helpers.py": "..."}. '
                "app.py is the entry and must listen on 0.0.0.0:$PORT. flask, "
                "waitress, fastapi, uvicorn, websockets, sqlalchemy, bcrypt, "
                "pyjwt, requests, httpx, jinja2, pillow and the stdlib are "
                "installed. Use fastapi+uvicorn instead of flask+waitress when "
                "the app needs WebSockets (multiplayer, live chat, anything "
                "pushed to clients) — waitress cannot do sockets. Only /data is "
                "writable and only /data survives a restart — put the database at "
                "/data/app.db. Routes are served under /bot/<name>/api/, with "
                "that prefix stripped: @app.get('/notes') answers at "
                "/bot/<name>/api/notes."
            ),
            "env": _str(
                'Secrets and config as JSON: {"API_KEY": "sk-..."}. Held outside '
                "the site directory, never served and never echoed back; read "
                "them with os.environ. Setting env restarts the server."
            ),
            "packages": _str(
                'Extra pip packages as a JSON list, e.g. ["redis==5.0.1"]. Only '
                "needed for something outside the installed set (flask, waitress, "
                "fastapi, uvicorn, websockets, sqlalchemy, bcrypt, pyjwt, requests, "
                "httpx, jinja2, pillow). Builds a per-site image, so the first "
                "deploy takes longer."
            ),
            "path": _str("For read: which server file (default app.py)"),
            "lines": _int("For logs: how many lines (default 40, max 200)"),
        },
        ["name", "action"],
    ),
    "delete_site": _obj(
        {"name": _str("Slug of the site to delete")},
        ["name"],
    ),
    "list_sites": _obj({}),
    "web_search": _obj(
        {
            "query": _str("Search query"),
            "max_results": _int("Optional result limit"),
            "engine": _str("Optional search engine hint"),
        },
        ["query"],
    ),
    "send_message": _obj(
        {
            "channel_id": _str(
                "Optional channel ID or DM recipient user ID to send to. If omitted, sends to current channel."
            ),
            "content": _str("Message text (Discord markdown OK)"),
            "reply": _bool(
                "Discord quote-reply (the quoted-parent UI). Default true. "
                "Pass false only for a standalone line with no quote. Keep it "
                "on when the room has moved on, several people are talking, "
                "or you are answering an older line."
            ),
            "reply_to": _str(
                "Optional short quote or who said it, like nah or alice. Not an id."
            ),
        },
        ["content"],
    ),
    # NOTE: no more `reasoning_log` tool. Reasoning now rides inside every
    # tool call via the auto-injected `reasoning` param (see build_openai_tools
    # above and tool_registry.record_reasoning). Plain chat goes through
    # send_message, which itself carries a reasoning field.
    "no_response": _obj({}),
    "more_tools": _obj(
        {
            "need": _str(
                "What you are trying to do, in a few words — 'ban a raider', "
                "'read my email', 'run a script'. Used to point you at the right tool."
            )
        }
    ),
    "send_file": _obj(
        {
            "filename": _str("File name with extension"),
            "content": _str("File contents (text or base64)"),
            "encoding": _str("text or base64"),
            "path": _str("Optional existing on-disk path instead of content"),
        }
    ),
    "shell": _obj(
        {
            "command": _str(
                "Bash command to run in the sandbox. Newlines are only allowed "
                "inside a heredoc. To write a file: cat << 'EOF' > path/file.py "
                "then the body then a line containing only EOF. Put `> file` on "
                "the opener line, not after EOF."
            ),
            "files": _str("Optional comma-separated files to attach after the command"),
        },
        ["command"],
    ),
    "sub_agent_message": _obj(
        {
            "run_id": _str(
                "The run_id the sub-agent gave you when it started. Find it in "
                "the 'Started sub-agent (run X)' reply."
            ),
            "text": _str("Message to the sub-agent: a question, a requirement, or a steer."),
        },
        ["run_id", "text"],
    ),
    "sub_agent_status": _obj(
        {
            "run_id": _str(
                "Optional. The run_id to inspect. Omit to list every live sub-agent "
                "run (status, step, elapsed) so you can see what's in flight."
            ),
        }
    ),
    "sub_agent": _obj(
        {
            "task": _str(
                "The complete task for the sub-agent. It cannot ask "
                "questions, so include the goal, any constraints, and how it "
                "should verify the result."
            ),
            "workdir": _str("Optional short name for the scratch directory"),
            "max_steps": _int("Optional cap on sub-agent steps (default 24)"),
            "mode": _str(
                "'background' (default for heavy work) returns immediately and "
                "the sub-agent runs on its own, posting the result when done — "
                "the turn is not blocked. 'foreground' waits here and hands the "
                "report back to you now. Use background unless you genuinely "
                "need the report to make the next decision this turn."
            ),
            "deliver": _str(
                "Where to post the result. 'channel' (default) posts to the "
                "channel where it was asked; 'dm' sends it to the person who "
                "asked, in a direct message. Use 'dm' when a long result would "
                "clutter a busy channel or when the work is private to them."
            ),
        },
        ["task"],
    ),
    "fetch_url": _obj(
        {
            "url": _str("URL to fetch"),
            "max_length": _int("Optional max characters of returned text"),
        },
        ["url"],
    ),
    "see_image": _obj(
        {
            "url": _str(
                "Image or GIF URL to look at: a direct jpg/png/gif/webp, "
                "a Discord CDN link, or a Tenor/Giphy/imgur GIF page"
            )
        },
        ["url"],
    ),
    "see_video": _obj(
        {
            "url": _str(
                "Direct mp4/webm/mov video URL to inspect with ffmpeg-derived "
                "frames; use youtube for YouTube links"
            )
        },
        ["url"],
    ),
    "youtube": _obj(
        {
            "url": _str(
                "YouTube video, channel, playlist, or search URL. "
                "Handles like @name also work."
            ),
            "query": _str("Optional YouTube search if url is omitted"),
            "limit": _int(
                "Optional max videos for channel/playlist/search (default 15)"
            ),
            "timestamps": _str("Optional comma-separated timestamps for frames"),
            "max_transcript_chars": _int("Optional transcript length cap"),
            "lang": _str("Optional caption language (default en)"),
        },
    ),
    "send_meme": _obj({"subreddit": _str("Optional subreddit name (e.g. me_irl)")}),
    "send_media": _obj(
        {"url": _str("Direct media URL to attach")},
        ["url"],
    ),
    "tts": _obj(
        {
            "text": _str("Text to speak"),
            "language": _str("Language name or code (e.g. english, spanish)"),
            "voice": _str(
                "TTS voice name (tiktok, mommy, or espanol/spanish). Omit for the default voice."
            ),
        },
        ["text"],
    ),
    "inbox_list": _obj({}),
    "inbox_act": _obj(
        {
            "action": _str(
                "accept, decline, dismiss, or read (read demotes a notice "
                "without clearing it)"
            ),
            "item_id": _str("Inbox item id, e.g. friend_123 or email_412"),
            "user_id": _str("Requester Discord id if item_id is omitted"),
        },
        ["action"],
    ),
    "join_vc": _obj(
        {
            "voice_channel_id": _str(
                "Numeric Discord voice channel id (snowflake, not a planner channel number)"
            ),
            "channel_name": _str("Voice channel name in the current server"),
            "user_id": _str("Join this user's current voice channel"),
        },
    ),
    "vc_status": _obj({}),
    "vc_where": _obj(
        {"user_id": _str("Numeric user ID or @mention")},
        ["user_id"],
    ),
    "leave_vc": _obj({}),
    "wait": _obj(
        {"seconds": _num("Pause this tool batch (default 2, max 10)")},
    ),
    "sleep": _obj(
        {"duration_minutes": _int("Sleep window in minutes (1-60, default 30)")},
    ),
    "clear_sleep": _obj({}),
    "update_base_personality": _obj(
        {"text": _str("New base personality (100-2000 chars)")},
        ["text"],
    ),
    "update_server_prompt": _obj(
        {
            "text": _str("New per-server prompt"),
            "server_id": _str("Optional guild id; defaults to the current server"),
        },
        ["text"],
    ),
    # maxwell@z3ki.dev email — local MTA. Bot talks to local Postfix
    # (127.0.0.1:25, SMTP+STARTTLS+SASL) and local Dovecot (127.0.0.1:993,
    # IMAPS+SASL). No third-party relay. See bot_tools.py and
    # email_integration/README.md.
    "email_send": _obj(
        {
            "to": _str(
                "Recipient(s). Comma-separated for multiple. e.g. 'a@x.com, b@y.com'"
            ),
            "subject": _str("Email subject line"),
            "body": _str("Plain text or HTML body (set is_html=true for HTML)"),
            "is_html": _bool("If true, body is sent as HTML. Default false."),
            "reply_to": _str("Optional Reply-To address"),
            "cc": _str("Optional comma-separated CC list"),
            "bcc": _str("Optional comma-separated BCC list"),
        },
        ["to", "subject", "body"],
    ),
    "email_read_inbox": _obj(
        {
            "max_results": _int("Max messages to return (default 10, max 50)"),
            "days_back": _int("Bound the window in days (default 7, max 90)"),
            "unread_only": _bool("If true, only show unread mail (default false)"),
        }
    ),
    "email_get_message": _obj(
        {
            "message_id": _str(
                "IMAP uid, from email_read_inbox, email_search, or an inbox "
                "email notice (412 and email_412 both work)"
            ),
            "max_chars": _int("Max body characters to return (default 8000)"),
        },
        ["message_id"],
    ),
    "email_search": _obj(
        {
            "query": _str("Free-text query, e.g. 'github', 'invoice', 'unsubscribe'"),
            "max_results": _int("Max matches to return (default 10, max 50)"),
        },
        ["query"],
    ),
    # X (Twitter). One read tool and one write tool — the action enum keeps
    # the catalog from growing six near-identical entries.
    "x_read": _obj(
        {
            "action": _str(
                "home (your feed), user (someone's posts), search, mentions "
                "(people talking to you), or tweet (one post by id/URL)",
                enum=["home", "user", "search", "mentions", "tweet"],
            ),
            "handle": _str("Account for action=user, with or without the @"),
            "query": _str(
                "Search text for action=search. X operators work: from:nasa, "
                "-filter:replies, min_faves:100, lang:en"
            ),
            "tweet_id": _str("Post id or full x.com URL, for action=tweet"),
            "limit": _int("How many posts (default 15, max 50)"),
        },
        ["action"],
    ),
    "x_post": _obj(
        {
            "action": _str(
                "post (new), reply, quote, delete, like, or repost",
                enum=["post", "reply", "quote", "delete", "like", "repost"],
            ),
            "text": _str("The post itself, for post/reply/quote"),
            "reply_to": _str("Post id or URL being replied to"),
            "quote": _str("Post id or URL being quoted"),
            "tweet_id": _str("Post id or URL for delete/like/repost"),
        },
        ["action"],
    ),

    # ---- Chess (Maxwell plays real chess against whoever starts a game) ----
    "chess_start": _obj(
        {
            "bot_side": _str(
                "white | black | auto (default white). The side Maxwell plays. "
                "The player gets the other colour."
            ),
            "depth": _int("Search depth for Maxwell's engine moves (1-4, default 3)"),
        }
    ),
    "chess_move": _obj(
        {
            "move": _str(
                "The move to play, in SAN (e4, Nf3, O-O, exd5, Qh5) or UCI "
                "(e2e4, e7e8q). If it is Maxwell's turn and move is omitted, "
                "Maxwell picks a move itself."
            ),
            "respond": _bool(
                "After a player move, automatically play Maxwell's reply in the "
                "same call (default true). Set false to play Maxwell's move "
                "separately."
            ),
        }
    ),
    "chess_state": _obj({}),
    "chess_resign": _obj(
        {
            "side": _str(
                "who resigns: maxwell | player (optional; default player)"
            ),
        }
    ),
    "usage": _obj({}),
}


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------
# The model had no way to know whether a tool hands its output back. That
# ignorance cost us real turns: it would answer *before* web_search returned
# ("here's what I found: ...") because it assumed the call was fire-and-forget,
# and it would sit waiting for a second turn after `react`/`typing`, which
# never produce one, leaving the channel silent.
#
# These three sets are the single source of truth. build_openai_tools() stamps
# the matching one-line contract onto every tool description, the system prompt
# lists tools grouped by contract, and bot.py imports RESULT_TOOL_NAMES to
# decide whether a batch loops back for another model turn. Add a new tool to
# exactly one set — the prompt, the schema, and the dispatch loop all follow.

# Tools whose output is fed back to the model in a fresh turn.
RESULT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "image_generator",
        "hd_image",
        "lookup_user",
        "search_messages",
        "create_invite",
        "join_server",
        "leave_server",
        "server_setup",
        "create_poll",
        "forward_message",
        "edit_message",
        # change_avatar returns a bare success ack ("Avatar changed
        # successfully"). Without a follow-up turn a model that calls only
        # this leaves the turn silent — the user sees the new avatar and no
        # text at all.
        "change_avatar",
        "list_servers",
        "create_site",
        "edit_site",
        "delete_site",
        "site_server",
        "list_sites",
        "web_search",
        "fetch_url",
        "see_image",
        "see_video",
        "youtube",
        "shell",
        "sub_agent",
        "sub_agent_message",
        "sub_agent_status",
        "list_admin_servers",
        "create_category",
        "create_channel",
        "edit_channel",
        "delete_channel",
        "kick_member",
        "ban_member",
        "unban_member",
        "list_bans",
        "timeout_member",
        "manage_role",
        "purge_messages",
        "pin_message",
        "set_member_nickname",
        "voice_mod",
        "lock_channel",
        "set_channel_permissions",
        "edit_server",
        "audit_log",
        "manage_emoji",
        "send_file",
        "send_meme",
        "send_media",
        # email_send is here too so a batch like email_send + send_message
        # still gets a second turn to confirm, retry, or react.
        "email_send",
        "email_read_inbox",
        "email_get_message",
        "email_search",
        "x_read",
        # x_post gets a turn back so he can say what he posted (and see the
        # link) instead of describing a post he has not confirmed landed.
        "x_post",
        "inbox_list",
        "inbox_act",
        "join_vc",
        "vc_status",
        "vc_where",
        "leave_vc",
        # set_activity gets a follow-up so the model can react to its own
        # status change. change_presence deliberately does NOT — that one is
        # the online/idle/dnd dot the user just set, and a follow-up turn
        # would race to undo it.
        "set_activity",
        "update_base_personality",
        "update_server_prompt",
        # more_tools hands the full catalog back and must get a turn to use it.
        "more_tools",
        # chess + usage return data the model needs a follow-up turn to react to.
        "chess_start",
        "chess_move",
        "chess_state",
        "chess_resign",
        "usage",
    }
)

# ── the conversational subset ─────────────────────────────────────────────
# Most turns are chat: "lol", "wdym", "what do you think of X". Shipping ~60
# tool schemas to answer those costs thousands of tokens per message and gives
# the model a shelf of moderation and server-admin machinery it has no use for.
# CHAT_CORE_TOOL_NAMES is what stays on an ordinary turn — talking, reacting,
# looking things up, seeing what was posted. Anything else arrives the moment
# the turn asks for it (see MaxwellBot._lean_chat_turn) or the moment the model
# calls more_tools, which hands back the full catalog and another turn.
CHAT_CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "send_message",
        "no_response",
        "react",
        "typing",
        "wait",
        "web_search",
        "fetch_url",
        "see_image",
        "see_video",
        "send_media",
        "send_meme",
        "image_generator",
        "more_tools",
        "chess_start",
        "chess_move",
        "chess_state",
        "chess_resign",
        "usage",
    }
)

# Tools that end the turn outright. Nothing after them runs, and there is no
# follow-up turn unless a RESULT tool shared the same batch.
TURN_ENDING_TOOL_NAMES: frozenset[str] = frozenset(
    {"send_message", "no_response", "sleep"}
)

# One line per contract class, appended to the tool's description so the model
# reads it in the same place it reads the parameters. Kept short on purpose —
# this text is paid for on every single request, for every single tool.
_CONTRACT_RESULT = (
    " [returns output: you get another turn with the result — "
    "do not describe or invent the result before you see it]"
)
_CONTRACT_ENDING = (
    " [ends the turn: nothing after it runs and you are not called again]"
)
_CONTRACT_SILENT = (
    " [returns nothing: no result, no extra turn — pair it with send_message "
    "in the SAME batch if the user should see a reply]"
)


def result_contract(name: str) -> str:
    """The one-line result contract appended to `name`'s description."""
    if name in RESULT_TOOL_NAMES:
        return _CONTRACT_RESULT
    if name in TURN_ENDING_TOOL_NAMES:
        return _CONTRACT_ENDING
    return _CONTRACT_SILENT


def contract_groups(names: list[str]) -> dict[str, list[str]]:
    """Split `names` into the three contract buckets, order preserved."""
    groups: dict[str, list[str]] = {"result": [], "ending": [], "silent": []}
    for name in names:
        if name in RESULT_TOOL_NAMES:
            groups["result"].append(name)
        elif name in TURN_ENDING_TOOL_NAMES:
            groups["ending"].append(name)
        else:
            groups["silent"].append(name)
    return groups


# The reasoning parameter is stamped onto EVERY tool so the model does its
# real reasoning *inside the tool call it wants to use* instead of a separate,
# pointless `reasoning_log` tool. Same shape everywhere — see tool_registry.py.
REASONING_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "One plain-English sentence (max ~280 chars) of WHY you are calling "
        "this tool — not the artifact, body, or output. Plain text only; no "
        "XML, JSON, or tags. First argument on every call, including send_message."
    ),
}


def build_openai_tools(
    tools: dict[str, Any],
    *,
    allowed_names: set[str] | None = None,
    disabled_names: set[str] | None = None,
    max_description_chars: int = 1024,
) -> list[dict[str, Any]]:
    """Build OpenAI ``tools`` payload from live tool instances.

    Every tool gets an auto-injected `reasoning` parameter on top of whatever
    it declared in TOOL_PARAMETERS. Reasoning lives INSIDE the tool call now —
    there is no standalone reasoning_log tool anymore. If you add a new tool,
    you do nothing special: it gets reasoning for free. Stop forgetting.

    Every description also gets its result contract appended (see
    result_contract) so the model knows, per tool, whether the output comes
    back to it. The contract is added AFTER the description is truncated, so
    a long description can never eat the part that changes the model's plan.
    """
    disabled = disabled_names or set()
    out: list[dict[str, Any]] = []
    for name, tool in tools.items():
        if name in disabled:
            continue
        if allowed_names is not None and name not in allowed_names:
            continue
        try:
            desc = str(tool.get_description() or "").strip()
        except Exception:
            desc = name
        if len(desc) > max_description_chars:
            desc = desc[: max_description_chars - 1] + "…"
        desc = (desc or name) + result_contract(name)
        params = dict(
            TOOL_PARAMETERS.get(
                name, {"type": "object", "properties": {}, "additionalProperties": True}
            )
        )
        # Inject reasoning onto a COPY so we never mutate TOOL_PARAMETERS.
        props = dict(params.get("properties") or {})
        props.setdefault("reasoning", REASONING_PARAM)
        params["properties"] = props
        # reasoning is ALWAYS required — no exceptions, no "terse on a trivial
        # call" carve-out. If the model thinks before it acts, we want the
        # trace. If it skips reasoning, the provider rejects the call instead
        # of silently dropping it (which is what bit us before).
        required = [r for r in (params.get("required") or []) if r != "reasoning"]
        if "reasoning" not in required:
            required.append("reasoning")
        params["required"] = required
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                },
            }
        )
    return out


def _decode_tool_arguments(raw_args: Any) -> dict[str, Any]:
    """Decode the argument shapes used by OpenAI-compatible providers.

    Providers are not consistent here: most send a JSON object string, some
    send an object directly, and a few double-encode the object or append
    harmless trailing markup. Keep that tolerance in one place so every
    native tool call reaches dispatch with the same ``dict`` shape.
    """
    import json

    if isinstance(raw_args, dict):
        return dict(raw_args)
    if not isinstance(raw_args, str):
        return {}

    text = raw_args.strip().lstrip("\ufeff")
    if not text:
        return {}

    # Unwrap at most two layers. This handles both:
    #   arguments='{"content":"hi"}'
    #   arguments='"{\\"content\\": \\"hi\\"}"'
    # and provider wrappers such as {"arguments": {...}}.
    current: Any = text
    parsed_json = False
    for _ in range(3):
        if isinstance(current, dict):
            if len(current) == 1:
                for wrapper in ("arguments", "parameters"):
                    if wrapper in current and isinstance(current[wrapper], (dict, str)):
                        current = current[wrapper]
                        break
                else:
                    return dict(current)
            else:
                return dict(current)
            continue
        if not isinstance(current, str):
            return {"_": current} if parsed_json else {}
        candidate = current.strip()
        try:
            current = json.loads(candidate)
            parsed_json = True
            continue
        except (json.JSONDecodeError, TypeError, ValueError):
            # raw_decode accepts a valid JSON object followed by provider
            # garbage, which has appeared in a few OpenAI-compatible streams.
            try:
                current, _end = json.JSONDecoder().raw_decode(candidate)
                parsed_json = True
                continue
            except (json.JSONDecodeError, TypeError, ValueError):
                break

    if isinstance(current, dict):
        return dict(current)
    if parsed_json:
        # Preserve the previous compatibility shape for valid JSON scalars.
        return {"_": current}

    # Last-resort compatibility for providers that emit a simple
    # ``key=value`` string instead of JSON. This is intentionally limited to
    # the old fallback behavior; it is never used for valid JSON.
    args: dict[str, Any] = {}
    for part in text.split():
        if "=" in part:
            key, value = part.split("=", 1)
            args[key.strip()] = value.strip().strip("\"'")
    if args:
        return args
    return {"content": text}


def normalize_native_tool_calls(raw_calls: list | None) -> list[dict[str, Any]]:
    """Normalize provider tool_calls into {id, name, arguments: dict, raw}."""
    normalized: list[dict[str, Any]] = []
    for i, call in enumerate(raw_calls or []):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(fn.get("name") or call.get("name") or "").strip()
        if not name:
            continue
        # Some providers use tool_ name prefixes
        if name.lower().startswith("tool_"):
            name = name[5:]
        raw_args = fn.get("arguments", call.get("arguments", {}))
        args = _decode_tool_arguments(raw_args)
        call_id = str(call.get("id") or f"call_{i}_{name}")
        normalized.append(
            {
                "id": call_id,
                "name": name,
                "arguments": args,
                "raw": call,
            }
        )
    return normalized


# ── text-form tool-call recovery ─────────────────────────────────────────
# Native `tools=` is the only dispatch protocol we ask for, but models still
# hand the call back as ORDINARY TEXT instead of a tool_calls entry. Every
# family does it in its own dialect:
#
#   GLM / Kimi     <tool_call>send_message
#                  <arg_key>content</arg_key><arg_value>hola</arg_value>
#   Qwen / vLLM    <function=send_message><parameter=content>hola</parameter>
#   DeepSeek DSML  <invoke name="send_message"><parameter name="content">…
#   gpt-oss        to=functions.send_message …{"content": "hola"}
#   bare JSON      {"name": "send_message", "arguments": {"content": "hola"}}
#
# Before this module the only handling was defensive scrubbing, and both of
# its outcomes were bad: a dialect the scrubber knew got deleted (the reply
# vanished and the turn went silent), and a dialect it did not know got
# posted to the channel verbatim — the user reading a raw parameter dump
# ("reasoning … content … reply true") instead of the message.
#
# Recovery beats scrubbing: parse the leaked markup back into a real tool
# call so it EXECUTES. send_message actually sends, shell actually runs, and
# whatever prose surrounded the markup survives as the leftover text.

_RECOVERY_MAX_CALLS = 8

_FENCE_RE = re.compile(r"```.*?(?:```|$)|~~~.*?(?:~~~|$)", re.DOTALL)

# One (key, value) pair inside a leaked call body, per dialect.
_PAIR_PATTERNS: tuple[re.Pattern, ...] = (
    # GLM-4.x / Kimi K2
    re.compile(
        r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>(.*?)(?:</arg_value>|$)",
        re.DOTALL | re.IGNORECASE,
    ),
    # Qwen / vLLM "<parameter=key>"
    re.compile(
        r"<parameter\s*=\s*([A-Za-z_]\w*)\s*>(.*?)(?:</parameter\s*>|$)",
        re.DOTALL | re.IGNORECASE,
    ),
    # Anthropic-style and DeepSeek DSML '<…parameter name="key" …>'
    re.compile(
        r"<[^<>]*parameter[^<>]*\bname\s*=\s*[\"']([^\"']+)[\"'][^<>]*>"
        r"(.*?)(?:</[^<>]*parameter[^<>]*>|$)",
        re.DOTALL | re.IGNORECASE,
    ),
    # '<arg>key</arg>value</arg>'
    re.compile(
        r"<arg>\s*([A-Za-z_]\w*)\s*</arg>(.*?)(?:</arg>|$)",
        re.DOTALL | re.IGNORECASE,
    ),
)

_TOOL_CALL_BLOCK_RE = re.compile(
    r"<tool_call\b[^>]*>(.*?)(?:</tool_call\s*>|$)", re.DOTALL | re.IGNORECASE
)
_FUNCTION_EQ_RE = re.compile(
    r"<function\s*=\s*([A-Za-z_][\w.]*)\s*>(.*?)(?:</function\s*>|$)",
    re.DOTALL | re.IGNORECASE,
)
_INVOKE_RE = re.compile(
    r"<[^<>]*invoke\b[^<>]*\bname\s*=\s*[\"']([^\"']+)[\"'][^<>]*>"
    r"(.*?)(?:</[^<>]*invoke[^<>]*>|$)",
    re.DOTALL | re.IGNORECASE,
)
# gpt-oss / harmony. Kept deliberately flat: the obvious pattern (an optional
# repeated "<|token|>" prefix glued to the name) nests quantifiers and takes
# seconds to fail on a reply that is mostly pipes. The header before the name
# is walked back separately, over a bounded window, so the leftover text is
# not a stray "<|channel|>commentary".
_HARMONY_RE = re.compile(r"\bto\s*=\s*functions?\.([A-Za-z_]\w*)", re.IGNORECASE)
_HARMONY_LEAD_RE = re.compile(r"<\|[^|<>]{0,32}\|>[^<>|]{0,32}$")
_HARMONY_LEAD_WINDOW = 96


def _harmony_call_start(text: str, pos: int) -> int:
    """Start of the "<|start|>assistant<|channel|>commentary " header at pos."""
    start = pos
    for _ in range(6):
        window = max(0, start - _HARMONY_LEAD_WINDOW)
        match = _HARMONY_LEAD_RE.search(text, window, start)
        if match is None or match.end() != start:
            break
        start = match.start()
    return start


# "send_message<arg>content</arg>hola</arg>" — a bare tool name glued to
# <arg> pairs, with no wrapper tag to find it by. Built per known-name set.
_BARE_ARG_BODY = r"((?:<arg>\s*[A-Za-z_]\w*\s*</arg>.*?</arg>)+)"
_bare_arg_cache: dict[frozenset, re.Pattern] = {}


def _bare_arg_call_re(names: frozenset) -> re.Pattern:
    cached = _bare_arg_cache.get(names)
    if cached is None:
        alt = (
            "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            or r"(?!x)x"
        )
        cached = re.compile(
            rf"(?<![A-Za-z0-9_])({alt})\s*{_BARE_ARG_BODY}",
            re.DOTALL | re.IGNORECASE,
        )
        _bare_arg_cache[names] = cached
    return cached


_NAME_ATTR_RE = re.compile(r"""\bname\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_BARE_NAME_LINE_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*$")
# `type` is first on OpenAI-shaped dumps (`{"type":"function","name":…}` /
# `{"type":"function","function":{…}}`). Still decoded by _args_from_json_obj,
# which ignores objects that have no tool name.
_JSON_OPEN_RE = re.compile(
    r'\{\s*"(?:name|tool|tool_name|function|type)"\s*:',
    re.IGNORECASE,
)
_JSON_FENCE_LANGS = frozenset({"", "json", "jsonc", "json5"})


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges covered by fenced code blocks (never tool calls)."""
    return [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def _json_only_fence_span(
    text: str, json_start: int, json_end: int, fences: list[tuple[int, int]]
) -> tuple[int, int] | None:
    """Fence span if this JSON object is the entire fenced body.

    Models (Grok especially) wrap a real tool-call JSON object in `````json``.
    Skipping that fence left the sanitizer to strip the object and post an
    empty `````json`` block to Discord. XML/tool markup inside a fence is
    still ignored — only a fence whose body is exactly one tool JSON counts.
    """
    json_body = text[json_start:json_end].strip()
    if not json_body:
        return None
    for fence_start, fence_end in fences:
        if not (fence_start <= json_start and json_end <= fence_end):
            continue
        block = text[fence_start:fence_end]
        if block.startswith("```"):
            mark = "```"
        elif block.startswith("~~~"):
            mark = "~~~"
        else:
            continue
        rest = block[len(mark) :]
        if rest.endswith(mark):
            rest = rest[: -len(mark)]
        lang, sep, body = rest.partition("\n")
        if not sep:
            body = lang
            lang = ""
            stripped = body.lstrip()
            lowered = stripped.lower()
            for prefix in ("jsonc", "json5", "json"):
                if lowered.startswith(prefix):
                    after = stripped[len(prefix) :].lstrip()
                    if after.startswith("{"):
                        lang = prefix
                        body = after
                    break
        if lang.strip().lower() not in _JSON_FENCE_LANGS:
            return None
        if body.strip() != json_body:
            return None
        return fence_start, fence_end
    return None


def _clean_tool_name(name: str) -> str:
    """Strip the wrappers providers put around a function name."""
    cleaned = str(name or "").strip().strip("\"'")
    for prefix in ("functions.", "tool_", "tools.", "namespace."):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.strip()


def _balanced_json_object(text: str, start: int) -> tuple[Any, int] | None:
    """Parse the JSON object beginning at ``start``; return (value, end)."""
    import json

    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1]), i + 1
                except (ValueError, TypeError):
                    return None
    return None


def _args_from_json_obj(obj: Any) -> tuple[str, dict[str, Any]] | None:
    """Pull (name, arguments) out of a decoded tool-call JSON object."""
    if not isinstance(obj, dict):
        return None
    lowered = {str(k).lower(): v for k, v in obj.items()}
    if isinstance(lowered.get("function"), dict):
        inner = _args_from_json_obj(lowered["function"])
        if inner:
            return inner
    name = ""
    for key in ("name", "tool", "tool_name", "function"):
        value = lowered.get(key)
        if isinstance(value, str) and value.strip():
            name = value
            break
    if not name:
        return None
    args: Any = {}
    for key in ("arguments", "parameters", "args", "input"):
        if key in lowered:
            args = lowered[key]
            break
    if isinstance(args, str):
        args = _decode_tool_arguments(args)
    if not isinstance(args, dict):
        args = {}
    return _clean_tool_name(name), dict(args)


def _pairs_from_body(body: str, name: str) -> dict[str, Any]:
    """Decode one leaked call body into an arguments dict.

    Dialects are tried in order and the FIRST one that yields any pair wins,
    so a body carrying both ``<arg_key>`` pairs and stray angle brackets in a
    value does not get shredded by the looser patterns below it.
    """
    for pattern in _PAIR_PATTERNS:
        found = pattern.findall(body)
        if found:
            return {
                str(key).strip(): value.strip()
                for key, value in found
                if str(key).strip()
            }
    match = _JSON_OPEN_RE.search(body) or re.search(r"\{", body)
    if match:
        parsed = _balanced_json_object(body, match.start())
        if parsed is not None:
            decoded = _args_from_json_obj(parsed[0])
            if decoded:
                return decoded[1]
            if isinstance(parsed[0], dict):
                return dict(parsed[0])
    # Last dialect: the tool's own parameter names used as XML tags,
    # e.g. "<content>hola</content>". Only names the schema declares are
    # accepted, so prose in angle brackets cannot invent an argument.
    props = dict((TOOL_PARAMETERS.get(name) or {}).get("properties") or {})
    props.setdefault("reasoning", REASONING_PARAM)
    args: dict[str, Any] = {}
    for key in props:
        tag = re.search(
            rf"<{re.escape(key)}\s*>(.*?)(?:</{re.escape(key)}\s*>|$)",
            body,
            re.DOTALL | re.IGNORECASE,
        )
        if tag:
            args[key] = tag.group(1).strip()
    return args


# The Python-call dialect: ``send_message(reasoning=…, content=…)``. Models
# emit it constantly, nothing recovered it, so the whole parameter dump landed
# in the channel as the visible reply.
#
# Two things make it awkward, and both show up in every real example:
#   * values are unquoted and full of commas — "content=quedó santo pa, ya
#     tenés el aura" is ONE value, so splitting on commas truncates the reply
#     mid-sentence;
#   * keys repeat — the model likes to restate `reasoning` after `content`.
# So a comma only ends a value when a DECLARED parameter name and `=` follow
# it, and the first occurrence of a key wins.
_PAREN_ARG_KEY_RE = re.compile(r"([A-Za-z_]\w*)\s*=")
_paren_call_cache: dict[frozenset, re.Pattern] = {}


def _paren_call_re(names: frozenset) -> re.Pattern:
    cached = _paren_call_cache.get(names)
    if cached is None:
        alt = (
            "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            or r"(?!x)x"
        )
        # The name must open the call and be followed immediately by
        # "key=", so ordinary prose containing "(" never matches.
        cached = re.compile(
            rf"(?<![A-Za-z0-9_])({alt})\s*\(\s*(?=[A-Za-z_]\w*\s*=)",
            re.IGNORECASE,
        )
        _paren_call_cache[names] = cached
    return cached


# ":)", ";-)", "=)" and friends. A smiley inside a value is not a closing
# paren, and Maxwell's rooms are full of them — without this, "content=mira
# esto :) jaja" gets cut to "mira esto :".
_EMOTICON_CLOSE_RE = re.compile(r"[:;=xX8]-?\)$")


def _balanced_paren_end(text: str, start: int) -> int:
    """Index just past the ``)`` closing the ``(`` at ``start``.

    Falls back to the end of the text when the model never closed it, which
    is better than dropping the call and posting the dump.
    """
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if _EMOTICON_CLOSE_RE.search(text[max(0, i - 2) : i + 1]):
                continue
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _strip_wrapping_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _pairs_from_paren_body(interior: str, name: str) -> dict[str, Any]:
    """Split ``key=value, key=value`` where values may contain commas."""
    props = dict((TOOL_PARAMETERS.get(name) or {}).get("properties") or {})
    props.setdefault("reasoning", REASONING_PARAM)
    marks: list[tuple[int, int, str]] = []
    for match in _PAREN_ARG_KEY_RE.finditer(interior):
        key = match.group(1)
        if key not in props:
            continue
        before = interior[: match.start()].rstrip()
        # A key only starts a new argument at the very front or right after
        # the comma that ended the previous one. Anything else is a "x=y"
        # that happens to live inside a value.
        if before and not before.endswith(","):
            continue
        marks.append((match.start(), match.end(), key))
    args: dict[str, Any] = {}
    for index, (_start, value_at, key) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(interior)
        value = interior[value_at:end].strip().rstrip(",").strip()
        # First occurrence wins: the trailing repeat is the model echoing
        # itself, and the leading one is what it actually reasoned with.
        if key not in args:
            args[key] = _strip_wrapping_quotes(value)
    return args


def _coerce_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Cast string values to the JSON types the schema declares.

    A recovered call arrives as text, so ``reply`` is the string ``"true"``
    and ``max_results`` is ``"5"``. Tools take those through kwargs, where a
    non-empty string is truthy — ``reply="false"`` would Discord-reply.
    """
    props = dict((TOOL_PARAMETERS.get(name) or {}).get("properties") or {})
    out: dict[str, Any] = {}
    for key, value in args.items():
        declared = props.get(key)
        kind = str((declared or {}).get("type") or "")
        if not isinstance(value, str) or not kind:
            out[key] = value
            continue
        text = value.strip()
        if kind == "boolean":
            lowered = text.lower()
            if lowered in {"true", "yes", "1", "on"}:
                out[key] = True
                continue
            if lowered in {"false", "no", "0", "off"}:
                out[key] = False
                continue
        elif kind in {"integer", "number"}:
            try:
                out[key] = int(text) if kind == "integer" else float(text)
                continue
            except (TypeError, ValueError):
                pass
        out[key] = value
    return out


# Keys of a leaked send_message whose tags were eaten before we saw the text
# (a markdown renderer, a scrubber, a client that swallows angle brackets),
# leaving a bare "key\nvalue" ladder as the visible reply.
_TAGLESS_KEYS = ("reasoning", "content", "reply", "reply_to")


def _recover_tagless_kv(text: str) -> tuple[str, dict[str, Any]] | None:
    """Recover a send_message whose markup was stripped down to key lines.

    Deliberately narrow: the text must OPEN on a bare parameter-name line and
    carry at least two of them including ``content``. A real reply that opens
    with a line reading only "reasoning" and later a line reading only
    "content" is not a thing anyone types.
    """
    lines = str(text or "").split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None or lines[first].strip().lower() not in _TAGLESS_KEYS:
        return None
    args: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[first:]:
        key = line.strip().lower()
        if key in _TAGLESS_KEYS and key not in args:
            current = key
            args[current] = []
            continue
        if current is None:
            return None
        args[current].append(line)
    if len(args) < 2 or "content" not in args:
        return None
    joined = {key: "\n".join(value).strip() for key, value in args.items()}
    if not joined.get("content"):
        return None
    return "send_message", joined


def _iter_gated(pattern: re.Pattern, text: str, lowered: str, marker: str):
    """Run ``pattern`` only when a cheap literal marker is present."""
    if marker not in lowered:
        return ()
    return pattern.finditer(text)


def recover_text_tool_calls(
    text: str, known_names: Any = None
) -> tuple[list[dict[str, Any]], str]:
    """Parse tool calls a model wrote as visible text.

    Returns ``(raw_calls, leftover_text)``. ``raw_calls`` are in the provider's
    own ``tool_calls`` shape, so callers can feed them straight into the same
    dispatch path a native call takes. ``leftover_text`` is the response with
    the recovered markup removed — the prose the model wrote around the call.

    Only names in ``known_names`` are recovered. XML/tool markup inside a
    fenced code block is skipped (quoting syntax is not a call). A fence
    whose body is exactly one tool-call JSON object is recovered — Grok
    wraps real calls in ``json`` fences, and skipping those left Discord
    an empty fence after the sanitizer ate the object.
    """
    import json

    raw = str(text or "")
    if not raw.strip():
        return [], raw
    allowed = {str(n).lower() for n in (known_names or ())} or None
    # Recovery runs on every reply the provider did not attach tool_calls to —
    # which is most of them. Cheap substring gates keep an ordinary chat
    # message from paying for six regex scans it can never match.
    lowered = raw.lower()
    fences = _fenced_spans(raw) if "```" in raw or "~~~" in raw else []

    found: list[tuple[int, int, str, dict[str, Any]]] = []

    def _add(start: int, end: int, name: str, args: dict[str, Any]) -> None:
        name = _clean_tool_name(name)
        if not name or _inside(start, fences):
            return
        if allowed is not None and name.lower() not in allowed:
            return
        found.append((start, end, name, args))

    for match in _iter_gated(_TOOL_CALL_BLOCK_RE, raw, lowered, "<tool_call"):
        body = match.group(1)
        name_attr = _NAME_ATTR_RE.search(match.group(0)[: match.group(0).find(">") + 1])
        name = name_attr.group(1) if name_attr else ""
        if not name:
            # "<tool_call>send_message\n<arg_key>…" — the name is whatever
            # leads the body, before the first tag or line break. Both the
            # newline-separated and the glued form show up in the wild.
            lead = re.split(r"[<\n]", body.lstrip(), maxsplit=1)[0]
            bare = _BARE_NAME_LINE_RE.match(lead)
            name = bare.group(1) if bare else ""
        if not name:
            json_match = _JSON_OPEN_RE.search(body)
            parsed = (
                _balanced_json_object(body, json_match.start()) if json_match else None
            )
            decoded = _args_from_json_obj(parsed[0]) if parsed else None
            if decoded:
                _add(match.start(), match.end(), decoded[0], decoded[1])
            continue
        _add(
            match.start(),
            match.end(),
            name,
            _pairs_from_body(body, _clean_tool_name(name)),
        )

    for pattern, marker in ((_FUNCTION_EQ_RE, "<function"), (_INVOKE_RE, "invoke")):
        for match in _iter_gated(pattern, raw, lowered, marker):
            name = _clean_tool_name(match.group(1))
            _add(
                match.start(), match.end(), name, _pairs_from_body(match.group(2), name)
            )

    if allowed and "<arg>" in lowered:
        for match in _bare_arg_call_re(frozenset(allowed)).finditer(raw):
            name = _clean_tool_name(match.group(1))
            _add(
                match.start(), match.end(), name, _pairs_from_body(match.group(2), name)
            )

    # send_message(reasoning=…, content=…) — a call written as a Python call.
    if allowed and "(" in raw and "=" in raw:
        for match in _paren_call_re(frozenset(allowed)).finditer(raw):
            name = _clean_tool_name(match.group(1))
            open_paren = raw.find("(", match.end(1))
            if open_paren == -1:
                continue
            end = _balanced_paren_end(raw, open_paren)
            interior = raw[
                open_paren + 1 : end - 1 if raw[end - 1 : end] == ")" else end
            ]
            args = _pairs_from_paren_body(interior, name)
            if args:
                _add(match.start(), end, name, args)

    for match in _iter_gated(_HARMONY_RE, raw, lowered, "functions."):
        name = _clean_tool_name(match.group(1))
        brace = raw.find("{", match.end())
        parsed = _balanced_json_object(raw, brace) if brace != -1 else None
        if parsed is None:
            continue
        args = parsed[0] if isinstance(parsed[0], dict) else {}
        decoded = _args_from_json_obj(args)
        _add(
            _harmony_call_start(raw, match.start()),
            parsed[1],
            name,
            decoded[1] if decoded else dict(args),
        )

    for match in _JSON_OPEN_RE.finditer(raw):
        parsed = _balanced_json_object(raw, match.start())
        if parsed is None:
            continue
        decoded = _args_from_json_obj(parsed[0])
        if not decoded:
            continue
        start, end = match.start(), parsed[1]
        wrapped = _json_only_fence_span(raw, start, end, fences)
        if wrapped:
            name = _clean_tool_name(decoded[0])
            if name and (allowed is None or name.lower() in allowed):
                found.append((wrapped[0], wrapped[1], name, decoded[1]))
            continue
        _add(start, end, decoded[0], decoded[1])

    # Drop overlaps (a <tool_call> wrapper and the JSON inside it both match),
    # keeping the outermost span so the wrapper is removed from the text too.
    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    kept: list[tuple[int, int, str, dict[str, Any]]] = []
    for span in found:
        if any(not (span[1] <= k[0] or span[0] >= k[1]) for k in kept):
            continue
        kept.append(span)
        if len(kept) >= _RECOVERY_MAX_CALLS:
            break
    kept.sort(key=lambda item: item[0])

    if not kept:
        tagless = _recover_tagless_kv(raw)
        if tagless is None:
            return [], raw
        name, args = tagless
        if allowed is not None and name.lower() not in allowed:
            return [], raw
        kept = [(0, len(raw), name, args)]

    leftover = raw
    for start, end, _name, _args in reversed(kept):
        leftover = leftover[:start] + leftover[end:]
    leftover = re.sub(r"\n{3,}", "\n\n", leftover).strip()

    calls: list[dict[str, Any]] = []
    for i, (_start, _end, name, args) in enumerate(kept):
        calls.append(
            {
                "id": f"recovered_{i}_{name}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(_coerce_args(name, args)),
                },
            }
        )
    return calls, leftover


# ── tool-loop transcript bounds ──────────────────────────────────────────
# Every agent loop in this repo (Discord, Telegram, sub_agent) replays the
# whole assistant/tool transcript on every round, so an unbounded tail is how a
# turn walks off the end of the context window mid-loop. Per-result truncation
# is not enough on its own: 24 rounds of a 32k-capped result is still ~768k
# chars riding on top of an already-full prompt.
TOOL_TAIL_MAX_MESSAGES = 24
TOOL_TAIL_MAX_CHARS = 96_000


def message_chars(message: dict) -> int:
    """Prompt size of one chat message, tool_calls included.

    An assistant turn replayed in a tool loop carries its arguments (a
    create_site body, a shell script, a long send_message) and those are real
    prompt tokens — counting only ``content`` leaves a budget blind to the
    heaviest messages in the conversation.
    """
    extra = 0
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if isinstance(fn, dict):
            extra += len(str(fn.get("name") or "")) + len(
                str(fn.get("arguments") or "")
            )
    content = message.get("content", "")
    if isinstance(content, str):
        return len(content) + extra
    if isinstance(content, list):
        return (
            sum(
                len(str(part.get("text", "")))
                for part in content
                if isinstance(part, dict)
            )
            + extra
        )
    return len(str(content or "")) + extra


def tool_tail_groups(tail: list[dict]) -> list[list[dict]]:
    """Split a tool-loop tail into (assistant, tool, tool, ...) rounds.

    A ``role: "tool"`` message is only valid while the assistant message that
    emitted its ``tool_call_id`` is still present, so grouping is what makes
    trimming safe.
    """
    groups: list[list[dict]] = []
    for msg in tail:
        if msg.get("role") == "tool" and groups:
            groups[-1].append(msg)
        else:
            groups.append([msg])
    return groups


def trim_tool_tail(
    tail: list[dict],
    *,
    max_messages: int = TOOL_TAIL_MAX_MESSAGES,
    max_chars: int = TOOL_TAIL_MAX_CHARS,
) -> list[dict]:
    """Bound a tool-loop tail by size AND count, oldest round first.

    Never slices mid-round: a plain ``tail[-24:]`` can cut an assistant message
    away from the ``role: "tool"`` replies carrying its tool_call_ids, which
    OpenAI-compatible providers reject with a 400 ("tool_call_id not found").
    Whole rounds are dropped instead, and the newest round always survives so
    the model still sees what it just ran.
    """
    groups = tool_tail_groups(tail)
    used = sum(message_chars(m) for m in tail)
    count = len(tail)
    while len(groups) > 1 and (count > max_messages or used > max_chars):
        dropped = groups.pop(0)
        count -= len(dropped)
        used -= sum(message_chars(m) for m in dropped)
    return [msg for group in groups for msg in group]


def elide_tool_calls_for_history(
    tool_calls: list[dict],
    *,
    heavy_keys: tuple[str, ...] = ("body", "content", "code", "html", "data"),
    max_chars: int = 2000,
) -> list[dict]:
    """Copy tool_calls with huge argument strings elided for context budget."""
    import copy
    import json

    out = copy.deepcopy(tool_calls or [])
    for call in out:
        fn = call.get("function")
        if not isinstance(fn, dict):
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                if len(raw_args) > max_chars:
                    fn["arguments"] = json.dumps(
                        {"_elided": f"[large arguments omitted, {len(raw_args)} chars]"}
                    )
                continue
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            continue
        if not isinstance(args, dict):
            continue
        changed = False
        for key in heavy_keys:
            val = args.get(key)
            if isinstance(val, str) and len(val) > max_chars:
                args[key] = f"[large {key} omitted, {len(val)} chars]"
                changed = True
        if changed:
            fn["arguments"] = json.dumps(args, ensure_ascii=False)
    return out
