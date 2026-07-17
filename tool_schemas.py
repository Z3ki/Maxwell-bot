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
    "reasoning_log": _obj(
        {
            "thoughts": _str("One plain-English sentence of reasoning"),
            "intent": _str("Short intent label"),
            "decision": _str("Short decision label"),
            "confidence": _str("low | medium | high"),
        },
        ["thoughts"],
    ),
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
    "send_meme": _obj(
        {"subreddit": _str("Optional subreddit name (e.g. me_irl)")}
    ),
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
}


def build_openai_tools(
    tools: dict[str, Any],
    *,
    allowed_names: set[str] | None = None,
    disabled_names: set[str] | None = None,
    max_description_chars: int = 1024,
) -> list[dict[str, Any]]:
    """Build OpenAI ``tools`` payload from live tool instances."""
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
        parameters = TOOL_PARAMETERS.get(
            name, {"type": "object", "properties": {}, "additionalProperties": True}
        )
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc or name,
                    "parameters": parameters,
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
