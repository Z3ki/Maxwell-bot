"""Modern Discord user-install features for Maxwell.

Keeps the core transport small while upgrading the personal-app surface with
richer slash options, extra message context actions, configurable live-channel
context, richer webhook payloads, and tool-disclosure support.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import user_install as ui

MESSAGE_EXPLAIN = "Explain"
MESSAGE_FACT_CHECK = "Fact-check"

_MODE_CHOICES = [
    {"name": "Ask", "value": "ask"},
    {"name": "Research", "value": "research"},
    {"name": "Summarize", "value": "summarize"},
    {"name": "Explain", "value": "explain"},
    {"name": "Rewrite", "value": "rewrite"},
    {"name": "Translate", "value": "translate"},
    {"name": "Brainstorm", "value": "brainstorm"},
    {"name": "Code", "value": "code"},
]
_WEB_CHOICES = [
    {"name": "Auto", "value": "auto"},
    {"name": "Search the web", "value": "search"},
    {"name": "Do not search", "value": "off"},
]
_DETAIL_CHOICES = [
    {"name": "Quick", "value": "quick"},
    {"name": "Balanced", "value": "balanced"},
    {"name": "Deep", "value": "deep"},
]
_CONTEXT_CHOICES = [
    {"name": "No channel context", "value": 0},
    {"name": "Last 10 messages", "value": 10},
    {"name": "Last 25 messages", "value": 25},
    {"name": "Last 50 messages", "value": 50},
]

_FEATURES_INSTALLED = False
_DISCLOSURE_INSTALLED = False
_ORIGINAL_BUILD_TURN = None
_ORIGINAL_SESSION_SEND = None


def modern_user_install_commands() -> list[dict[str, Any]]:
    """Return the current USER_INSTALL command set."""

    meta = {"integration_types": [1], "contexts": [0, 1, 2]}
    return [
        {
            "name": ui.USER_INSTALL_COMMAND_NAME,
            "description": "Ask Maxwell with tools, files, web research, and live context.",
            "type": 1,
            **meta,
            "options": [
                {
                    "name": "prompt",
                    "description": "What you want Maxwell to do",
                    "type": 3,
                    "required": True,
                    "max_length": 4000,
                },
                {
                    "name": "mode",
                    "description": "How Maxwell should approach the request",
                    "type": 3,
                    "required": False,
                    "choices": _MODE_CHOICES,
                },
                {
                    "name": "web",
                    "description": "Control web research for this request",
                    "type": 3,
                    "required": False,
                    "choices": _WEB_CHOICES,
                },
                {
                    "name": "detail",
                    "description": "How much detail to return",
                    "type": 3,
                    "required": False,
                    "choices": _DETAIL_CHOICES,
                },
                {
                    "name": "language",
                    "description": "Response/translation language, e.g. Spanish or English",
                    "type": 3,
                    "required": False,
                    "max_length": 80,
                },
                {
                    "name": "context",
                    "description": "Recent channel messages to include when Maxwell can read them",
                    "type": 4,
                    "required": False,
                    "choices": _CONTEXT_CHOICES,
                },
                {
                    "name": "image",
                    "description": "Optional image for Maxwell to inspect",
                    "type": 11,
                    "required": False,
                },
                {
                    "name": "file",
                    "description": "Optional file for Maxwell to read",
                    "type": 11,
                    "required": False,
                },
            ],
        },
        {
            "name": ui.USER_INSTALL_MESSAGE_ASK,
            "type": 3,
            **meta,
        },
        {
            "name": ui.USER_INSTALL_MESSAGE_SUMMARIZE,
            "type": 3,
            **meta,
        },
        {
            "name": MESSAGE_EXPLAIN,
            "type": 3,
            **meta,
        },
        {
            "name": MESSAGE_FACT_CHECK,
            "type": 3,
            **meta,
        },
        {
            "name": ui.USER_INSTALL_USER_ASK,
            "type": 2,
            **meta,
        },
    ]


def _options(interaction: Any) -> dict[str, Any]:
    data = ui._interaction_data(interaction)
    return {name: value for name, value in ui._option_pairs(data.get("options"))}


def _context_limit(interaction: Any) -> int:
    raw = _options(interaction).get("context", 25)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 25
    return value if value in {0, 10, 25, 50} else 25


def _slash_prompt(prompt: str, options: dict[str, Any]) -> str:
    """Add explicit per-command intent without changing the user's text."""

    mode = str(options.get("mode") or "ask").strip().lower()
    web = str(options.get("web") or "auto").strip().lower()
    detail = str(options.get("detail") or "balanced").strip().lower()
    language = str(options.get("language") or "").strip()[:80]

    mode_instruction = {
        "ask": "",
        "research": (
            "Research this request before answering. Use current, credible sources "
            "when external facts matter, prefer primary sources, and distinguish "
            "verified facts from uncertainty."
        ),
        "summarize": (
            "Summarize the supplied material. Preserve the important facts, decisions, "
            "numbers, caveats, and action items; do not invent missing context."
        ),
        "explain": (
            "Explain this clearly and concretely. Define necessary jargon, show the "
            "reasoning structure, and use examples when they improve understanding."
        ),
        "rewrite": (
            "Rewrite the supplied material while preserving its intended meaning. "
            "Improve clarity, structure, and wording instead of adding new claims."
        ),
        "translate": (
            "Translate the supplied material accurately and naturally. Preserve names, "
            "numbers, code, links, and formatting where practical."
        ),
        "brainstorm": (
            "Brainstorm useful, distinct ideas for this request. Prefer concrete options "
            "with tradeoffs over repetitive variants."
        ),
        "code": (
            "Treat this as a coding/technical task. Inspect available context before "
            "assuming details, use tools when useful, and return a concrete implementation "
            "or debugging result."
        ),
    }.get(mode, "")

    instructions = []
    if mode_instruction:
        instructions.append(mode_instruction)
    if web == "search":
        instructions.append(
            "Use web_search before the final answer when the request depends on external "
            "or current information, and ground those claims in the sources you found."
        )
    elif web == "off":
        instructions.append(
            "Do not use web_search or fetch_url for this request; work from the supplied "
            "conversation, attachments, and local context."
        )

    if detail == "quick":
        instructions.append("Keep the final answer concise and focused on the result.")
    elif detail == "deep":
        instructions.append(
            "Give a thorough answer with the important reasoning, caveats, and implementation details."
        )

    if language:
        if mode == "translate":
            instructions.append(f"Target language: {language}.")
        else:
            instructions.append(f"Respond in {language}.")

    if not instructions:
        return prompt
    return "\n".join(instructions) + "\n\nUser request:\n" + prompt


def _enhanced_build_turn(interaction: Any, original_build: Any) -> dict[str, Any] | None:
    data = ui._interaction_data(interaction)
    name = str(data.get("name") or "")
    cmd_type = data.get("type")
    cmd_type = getattr(cmd_type, "value", cmd_type)
    try:
        cmd_type = int(cmd_type) if cmd_type is not None else 1
    except (TypeError, ValueError):
        cmd_type = 1

    if cmd_type == 3 and name in {MESSAGE_EXPLAIN, MESSAGE_FACT_CHECK}:
        target = ui.parse_target_message(interaction)
        if target is None:
            return None
        if name == MESSAGE_EXPLAIN:
            prompt = (
                "Explain this message clearly. Use the pointed-at message as the source "
                "material; explain context, jargon, implications, and any code/error text "
                "without inventing missing details."
            )
        else:
            prompt = (
                "Fact-check the claims in this message. Use web search for current or "
                "externally verifiable claims, prefer reliable/primary sources, and separate "
                "verified facts, unsupported claims, contradictions, and uncertainty."
            )
        channel = getattr(interaction, "channel", None)
        channel_name = str(getattr(channel, "name", "") or "") or "this channel"
        return {
            "prompt": prompt,
            "attachments": [],
            "mentions": list(getattr(target, "mentions", None) or []),
            "reference": SimpleNamespace(
                message_id=getattr(target, "id", None),
                resolved=target,
            ),
            "note": (
                f"User-install message action ({name}) in #{channel_name}. "
                "The pointed-at message is the reply parent. Channel transcript may be "
                "incomplete unless Maxwell is also in this server."
            ),
            "command": name,
        }

    turn = original_build(interaction)
    if turn is None:
        return None
    if cmd_type == 1 and name == ui.USER_INSTALL_COMMAND_NAME:
        opts = _options(interaction)
        turn["prompt"] = _slash_prompt(str(turn.get("prompt") or ""), opts)
        turn["history_limit"] = _context_limit(interaction)
        mode = str(opts.get("mode") or "ask")
        web = str(opts.get("web") or "auto")
        detail = str(opts.get("detail") or "balanced")
        turn["note"] = (
            str(turn.get("note") or "")
            + f" Personal-app options: mode={mode}, web={web}, detail={detail}, "
            f"context={turn['history_limit']}."
        ).strip()
    return turn


async def _snapshot_channel_history(bot: Any, interaction: Any) -> list[dict[str, Any]]:
    """Read only the amount of live channel context selected by the user."""

    limit = _context_limit(interaction)
    if limit <= 0:
        return []
    cid = getattr(interaction, "channel_id", None)
    channel = getattr(interaction, "channel", None)
    history = getattr(channel, "history", None)
    if not callable(history) and cid is not None and bot is not None:
        getter = getattr(bot, "get_channel", None)
        if callable(getter):
            with_ch = getter(int(cid) if str(cid).isdigit() else cid)
            if with_ch is not None:
                channel = with_ch
                history = getattr(channel, "history", None)
        if not callable(history):
            fetch = getattr(bot, "fetch_channel", None)
            if callable(fetch):
                try:
                    channel = await fetch(int(cid) if str(cid).isdigit() else cid)
                    history = getattr(channel, "history", None)
                except Exception:
                    history = None
    if not callable(history):
        return []

    rows: list[dict[str, Any]] = []
    try:
        result = history(limit=limit)
        if hasattr(result, "__aiter__"):
            rows.extend([ui._memory_row_from_message(msg) async for msg in result])
        else:
            rows.extend(ui._memory_row_from_message(msg) for msg in result or [])
    except Exception as exc:
        ui.logger.info("user-install channel history unavailable: %s", exc)
        return []
    rows.reverse()
    return rows


async def _modern_session_send(
    self: Any,
    content: str | None = None,
    file: Any = None,
    **kwargs: Any,
) -> Any:
    """Webhook send that preserves modern Discord payload features."""

    await self.ensure_deferred()
    for ignored in ("stickers", "reference", "mention_author"):
        kwargs.pop(ignored, None)

    text = None if content is None else str(content)
    if text == "":
        text = None
    if self._sent >= ui.USER_INSTALL_MESSAGE_CAP:
        return await self._edit_last(text, file)

    followup = getattr(self.interaction, "followup", None)
    send = getattr(followup, "send", None)
    if not callable(send):
        raise TypeError("user-install interaction has no followup.send")

    payload: dict[str, Any] = {}
    if text is not None:
        payload["content"] = text

    extra_files = kwargs.pop("files", None)
    if file is not None and extra_files:
        payload["files"] = [file, *list(extra_files)]
    elif file is not None:
        payload["file"] = file
    elif extra_files:
        payload["files"] = list(extra_files)

    embed = kwargs.pop("embed", None)
    embeds = kwargs.pop("embeds", None)
    if embed is not None:
        payload["embed"] = embed
    elif embeds:
        payload["embeds"] = list(embeds)

    # discord.Webhook.send-compatible fields that are useful for personal-app
    # replies. Unknown bot/channel-only kwargs are intentionally dropped.
    for key in (
        "view",
        "allowed_mentions",
        "suppress_embeds",
        "silent",
        "tts",
        "poll",
        "ephemeral",
        "delete_after",
    ):
        if key in kwargs and kwargs[key] is not None:
            payload[key] = kwargs[key]

    if not payload:
        payload["content"] = "\u200b"
    sent = await send(**payload)
    self._sent += 1
    self._last = sent
    return sent


def install_user_install_features(bot: Any) -> None:
    """Upgrade commands and transport before Discord command sync happens."""

    del bot  # install is process-wide because user_install is a shared transport module.
    global _FEATURES_INSTALLED, _ORIGINAL_BUILD_TURN, _ORIGINAL_SESSION_SEND
    if _FEATURES_INSTALLED:
        return

    ui.USER_INSTALL_MESSAGE_EXPLAIN = MESSAGE_EXPLAIN
    ui.USER_INSTALL_MESSAGE_FACT_CHECK = MESSAGE_FACT_CHECK
    ui.USER_INSTALL_COMMANDS[:] = modern_user_install_commands()
    ui.USER_INSTALL_NAMES = frozenset(
        {
            ui.USER_INSTALL_COMMAND_NAME,
            ui.USER_INSTALL_MESSAGE_ASK,
            ui.USER_INSTALL_MESSAGE_SUMMARIZE,
            ui.USER_INSTALL_USER_ASK,
            MESSAGE_EXPLAIN,
            MESSAGE_FACT_CHECK,
        }
    )

    _ORIGINAL_BUILD_TURN = ui.build_user_install_turn

    def build_turn(interaction: Any) -> dict[str, Any] | None:
        return _enhanced_build_turn(interaction, _ORIGINAL_BUILD_TURN)

    ui.build_user_install_turn = build_turn
    ui.snapshot_channel_history = _snapshot_channel_history
    # Payload-preserving send is the host UserInstallSession._send_impl.
    _FEATURES_INSTALLED = True


def install_user_install_tool_disclosure(bot: Any) -> None:
    """Attach the existing Tools · N audit button to user-install follow-ups."""

    global _DISCLOSURE_INSTALLED
    if _DISCLOSURE_INSTALLED:
        return
    from . import audit_ui

    def factory(original):
        async def disclosed_send(
            self: Any,
            content: str | None = None,
            file: Any = None,
            **kwargs: Any,
        ) -> Any:
            sent = await original(self, content=content, file=file, **kwargs)
            inbound = SimpleNamespace(id=getattr(self.interaction, "id", 0))
            with contextlib.suppress(Exception):
                await audit_ui._attach_trace(bot, sent, inbound)
            return sent

        disclosed_send._maxwell_user_install_disclosure = True  # type: ignore[attr-defined]
        return disclosed_send

    # wrap_session_send rebuilds UserInstallSession.send from _send_impl.
    # Patching .send directly is wiped when embed/progress wrappers install.
    ui.wrap_session_send(factory, name="tool_disclosure", priority=80)
    _DISCLOSURE_INSTALLED = True


__all__ = [
    "MESSAGE_EXPLAIN",
    "MESSAGE_FACT_CHECK",
    "install_user_install_features",
    "install_user_install_tool_disclosure",
    "modern_user_install_commands",
]
