"""OpenAI-compatible tool schemas for Maxwell native function calling.

Each entry maps a tool name to a JSON Schema ``parameters`` object. Descriptions
come from the live tool instances at request time so they stay in sync with
``get_description()``.
"""

from __future__ import annotations

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
        {"prompt": _str("HD image generation prompt")},
        ["prompt"],
    ),
    "memory_edit": _obj(
        {
            "action": _str("add | edit | remove"),
            "content": _str("Memory text (required for add and edit)"),
            "memory_id": _str("Memory id (required for edit and remove)"),
        },
        ["action"],
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
        {"message_id": _str("Message ID to delete")},
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
    "list_admin_servers": _obj({}),
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
            "topic": _str("New topic"),
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
    "change_avatar": _obj(
        {"url": _str("Direct image URL (jpg/png/gif/webp)")},
        ["url"],
    ),
    "create_site": _obj(
        {
            "name": _str("Short slug: lowercase, numbers, hyphens"),
            "title": _str("Site title / headline"),
            "body": _str(
                "FULL HTML document (DOCTYPE through closing tags). "
                "Prefer this over stuffing HTML into chat."
            ),
            "encoding": _str("text (default) or base64 for exact bytes"),
            "images": _str("Optional JSON list of local image paths to include"),
        },
        ["name", "title", "body"],
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
            "content": _str("Message text (Discord markdown OK)"),
            "reply": _bool("Whether to reply to the triggering message (default true)"),
        },
        ["content"],
    ),
    # NOTE: no more `reasoning_log` tool. Reasoning now rides inside every
    # tool call via the auto-injected `reasoning` param (see build_openai_tools
    # above and tool_registry.record_reasoning). Plain chat goes through
    # send_message, which itself carries a reasoning field.
    "no_response": _obj({}),
    "send_file": _obj(
        {
            "filename": _str("File name with extension"),
            "content": _str("File contents (text or base64)"),
            "encoding": _str("text or base64"),
            "path": _str("Optional existing on-disk path instead of content"),
        }
    ),
    "shell": _obj(
        {"command": _str("Bash command to run in the sandbox container")},
        ["command"],
    ),
    "fetch_url": _obj(
        {
            "url": _str("URL to fetch"),
            "max_length": _int("Optional max characters of returned text"),
        },
        ["url"],
    ),
    "youtube": _obj(
        {
            "url": _str("YouTube video URL"),
            "timestamps": _str("Optional comma-separated timestamps for frames"),
            "max_transcript_chars": _int("Optional transcript length cap"),
        },
        ["url"],
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
        },
        ["text"],
    ),
    "leave_vc": _obj({}),
    "sub_agent": _obj(
        {
            "task": _str("Task description for the background sub-agent"),
            "slug": _str("Optional short slug for the sub-agent workdir"),
            "timeout_minutes": _int("Optional timeout"),
            "files": _str(
                "Optional JSON list of local file paths to expose to the sub-agent"
            ),
        },
        ["task"],
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
            "message_id": _str("Message id (from email_read_inbox or email_search)"),
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
}


# The reasoning parameter is stamped onto EVERY tool so the model does its
# real reasoning *inside the tool call it wants to use* instead of a separate,
# pointless `reasoning_log` tool. Same shape everywhere — see tool_registry.py.
REASONING_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "Your real, plain-English reasoning BEFORE you take this action: why "
        "you are calling this tool, what you expect to happen, assumptions and "
        "risks. Plain text only — no XML, no JSON, no tags. Fill this in for "
        "EVERY tool call including send_message.\n\n"
        "Scale the length to the task: trivial actions (react, sleep, clear_sleep) "
        "get ONE short sentence (~30-80 chars). Routine tool calls (send_message, "
        "send_file, fetch_url, create_poll) get 1-2 sentences (~80-200 chars). "
        "Complex or multi-step tasks (create_site with custom HTML, shell with a "
        "non-obvious command, image_generator with a detailed prompt, multi-tool "
        "plans, debugging, anything where you considered alternatives) get a real "
        "paragraph: 3-6 sentences, 300-900 chars. Walk through WHY this tool, "
        "what you expect, what you'd do if it fails, and any sub-decisions. The "
        "user sees this reasoning stream live in the channel while you work, so "
        "longer is better when the work is non-trivial. The 2026-07-19 directive: "
        "the bot was shipping 'looking up x' one-liners on jobs that clearly needed "
        "real thought; users couldn't tell what was happening or whether to trust "
        "the call. Reason like a senior engineer writing a PR description — short "
        "for trivial, full paragraph for non-trivial. The dashboard caps at 2000 chars."
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
                    "description": desc or name,
                    "parameters": params,
                },
            }
        )
    return out


def normalize_native_tool_calls(raw_calls: list | None) -> list[dict[str, Any]]:
    """Normalize provider tool_calls into {id, name, arguments: dict, raw}."""
    import json

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
        args: dict[str, Any]
        if isinstance(raw_args, dict):
            args = dict(raw_args)
        elif isinstance(raw_args, str):
            text = raw_args.strip()
            if not text:
                args = {}
            else:
                try:
                    parsed = json.loads(text)
                    args = dict(parsed) if isinstance(parsed, dict) else {"_": parsed}
                except json.JSONDecodeError:
                    # Best-effort key=value fallback
                    args = {}
                    for part in text.split():
                        if "=" in part:
                            k, v = part.split("=", 1)
                            args[k.strip()] = v.strip().strip("\"'")
                    if not args:
                        args = {"content": text}
        else:
            args = {}
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
