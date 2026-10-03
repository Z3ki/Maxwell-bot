"""Modern Discord user-install features for Maxwell.

Keeps the core transport small while upgrading the personal-app surface with
richer slash options, extra message context actions, configurable live-channel
context, and richer webhook payloads.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

import user_install as ui

MESSAGE_EXPLAIN = "Explain"
MESSAGE_FACT_CHECK = "Fact-check"
MESSAGE_TRANSFORM = "Rewrite / Translate"
logger = logging.getLogger(__name__)

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
    {"name": "Brief", "value": "quick"},
    {"name": "Balanced", "value": "balanced"},
    {"name": "Deep", "value": "deep"},
]
_CONTEXT_CHOICES = [
    {"name": "No channel context" if count == 0 else f"Last {count:,} messages", "value": count}
    for count in ui.USER_INSTALL_CONTEXT_COUNTS
]

_FEATURES_INSTALLED = False
_CORE_SNAPSHOT_CHANNEL_HISTORY = ui.snapshot_channel_history
_ORIGINAL_BUILD_TURN = None
_USER_PREFERENCE_STORE = None


def set_user_preference_store(store: Any) -> None:
    """Make personal defaults available to the slash turn builder."""
    global _USER_PREFERENCE_STORE
    _USER_PREFERENCE_STORE = store


def modern_user_install_commands() -> list[dict[str, Any]]:
    """Return the current USER_INSTALL command set."""

    # `/maxwell` works when installed to a server and when installed to a user.
    meta = {"integration_types": [0, 1], "contexts": [0, 1, 2]}
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
                    "description": "Auto searches current facts; or choose Search or Do not search",
                    "type": 3,
                    "required": False,
                    "choices": _WEB_CHOICES,
                },
                {
                    "name": "detail",
                    "description": "Brief by default; choose Balanced or Deep for more explanation",
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
                    "name": "visibility",
                    "description": "Public by default; choose Private or save your default in /config",
                    "type": 3,
                    "required": False,
                    "choices": [
                        {"name": "Private", "value": "private"},
                        {"name": "Public", "value": "public"},
                    ],
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
            "name": MESSAGE_TRANSFORM,
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


def _context_limit(interaction: Any, *, options: dict[str, Any] | None = None) -> int:
    opts = _options(interaction) if options is None else options
    if options is None and "context" not in opts and _USER_PREFERENCE_STORE is not None:
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        try:
            opts["context"] = _USER_PREFERENCE_STORE.get(user_id)["defaults"].get("context", 25)
        except Exception:
            opts["context"] = 0
    return ui.normalize_context_limit(opts.get("context", ui.USER_INSTALL_HISTORY_LIMIT))

def _slash_prompt(prompt: str, options: dict[str, Any]) -> str:
    """Add explicit per-command intent without changing the user's text."""

    mode = str(options.get("mode") or "ask").strip().lower()
    web = str(options.get("web") or "auto").strip().lower()
    detail = str(options.get("detail") or "quick").strip().lower()
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
            "Search the web before answering this request. Use the sources you find, "
            "cite their URLs, and say when the results do not verify an answer."
        )
    elif web == "off":
        instructions.append(
            "Do not use web_search or fetch_url for this request; work from the supplied "
            "conversation, attachments, and local context."
        )
    else:
        instructions.append(
            "Do not search unless this request needs a live external fact or the user "
            "asked for a lookup. Do not add source links when you did not use a result."
        )

    if detail == "quick":
        instructions.append(
            "Keep the final answer concise and focused on the result. Usually use 1-3 short "
            "sentences. Skip introductions, repeated points, and unnecessary explanation. "
            "Use more space only when the request needs it or the user asks for detail."
        )
    elif detail == "balanced":
        instructions.append("Give a focused answer with enough explanation to be useful.")
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

    turn = original_build(interaction)
    if turn is None:
        return None
    opts = _options(interaction)
    user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
    if _USER_PREFERENCE_STORE is not None and user_id:
        try:
            defaults = _USER_PREFERENCE_STORE.get(user_id).get("defaults") or {}
        except Exception:
            defaults = {"visibility": "private", "context": 0}
        for key in ("mode", "web", "detail", "context", "language", "visibility"):
            if key not in opts and key in defaults:
                opts[key] = defaults[key]
    if cmd_type == 3:
        # A quick action chooses its task; the modal can explicitly override it.
        action_mode = {
            ui.USER_INSTALL_MESSAGE_ASK: "ask",
            ui.USER_INSTALL_MESSAGE_SUMMARIZE: "summarize",
            MESSAGE_EXPLAIN: "explain",
            MESSAGE_FACT_CHECK: "research",
            MESSAGE_TRANSFORM: "rewrite",
        }.get(name, "ask")
        opts["mode"] = _options(interaction).get("mode", action_mode)
        if name == MESSAGE_FACT_CHECK:
            opts["web"] = "search"
            turn["prompt"] = "Fact-check the claims in the selected message. Cite primary sources and identify uncertainty."
        elif name == MESSAGE_EXPLAIN:
            turn["prompt"] = "Explain the selected message clearly without inventing missing details."
        elif name == MESSAGE_TRANSFORM:
            turn["prompt"] = "Rewrite the selected message while preserving its meaning."
        if data.get("request_prompt"):
            turn["prompt"] = str(data["request_prompt"]).strip()
    raw_prompt = str(turn.get("prompt") or "")
    target = getattr(turn.get("reference"), "resolved", None)
    turn["search_query"] = str(getattr(target, "content", "") or raw_prompt)
    turn["prompt"] = _slash_prompt(raw_prompt, opts)
    turn["history_limit"] = _context_limit(interaction, options=opts)
    turn["visibility"] = ui.resolve_response_visibility(interaction, defaults=opts)
    turn["mode"] = str(opts.get("mode") or "ask").strip().lower()
    turn["web"] = str(opts.get("web") or "auto").strip().lower()
    detail = str(opts.get("detail") or "quick")
    turn["note"] = (
        str(turn.get("note") or "")
        + f" Personal-app options: mode={turn['mode']}, web={turn['web']}, "
        f"detail={detail}, context={turn['history_limit']}."
    ).strip()
    return turn


def _saved_visibility(interaction: Any, *, fallback: str = "public") -> str:
    defaults = None
    user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
    if _USER_PREFERENCE_STORE is not None and user_id:
        try:
            defaults = _USER_PREFERENCE_STORE.get(user_id).get("defaults") or {}
        except Exception:
            defaults = {"visibility": "private"}
    return ui.resolve_response_visibility(interaction, defaults=defaults, fallback=fallback)


async def _snapshot_channel_history(
    bot: Any, interaction: Any, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Use the core bounded history reader with this request's preferences."""
    limit = _context_limit(interaction) if limit is None else ui.normalize_context_limit(limit)
    return await _CORE_SNAPSHOT_CHANNEL_HISTORY(bot, interaction, limit=limit)


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
        "poll",
        "ephemeral",
        "delete_after",
    ):
        if key in kwargs and kwargs[key] is not None:
            payload[key] = kwargs[key]

    payload["allowed_mentions"] = discord.AllowedMentions.none()
    if not payload:
        payload["content"] = "\u200b"
    payload["ephemeral"] = bool(getattr(self, "ephemeral", True))
    sent = await send(**payload)
    self._sent += 1
    self._last = sent
    return sent



class _MessageRequestInteraction:
    def __init__(self, interaction: Any, target: Any, prompt: str, options: list[dict]):
        self._interaction = interaction
        self.type = 2
        self._maxwell_modal_request = True
        self.data = {
            "name": ui.USER_INSTALL_MESSAGE_ASK, "type": 3,
            "target_id": str(target.id), "resolved": {"messages": {str(target.id): target}},
            "request_prompt": prompt, "options": options,
        }

    def __getattr__(self, key):
        return getattr(self._interaction, key)


class _MessageRequestModal(discord.ui.Modal):
    def __init__(self, bot: Any, interaction: Any, target: Any, *, mode: str = "ask"):
        super().__init__(title="Ask Maxwell about this message", timeout=300)
        self.bot = bot
        self.target = target
        self.scope = ui.private_channel_key(interaction)
        self._submitted = False
        self.prompt = discord.ui.TextInput(
            placeholder="For example: explain this error or draft a reply", required=False,
            max_length=4000, style=discord.TextStyle.paragraph,
        )
        self.mode = discord.ui.Select(options=[
            discord.SelectOption(label=row["name"], value=row["value"], default=row["value"] == mode)
            for row in _MODE_CHOICES
        ])
        self.detail = discord.ui.Select(options=[
            discord.SelectOption(label="My saved detail", value="saved", default=True),
            *[discord.SelectOption(label=row["name"], value=row["value"]) for row in _DETAIL_CHOICES],
        ])
        self.visibility = discord.ui.Select(options=[
            discord.SelectOption(label="My saved visibility", value="saved", default=True),
            discord.SelectOption(label="Public", value="public"),
            discord.SelectOption(label="Private — only me", value="private"),
        ])
        self.language = discord.ui.TextInput(placeholder="For translation, e.g. Spanish", required=False, max_length=80)
        for text, item in [("Your request (optional)", self.prompt), ("Action", self.mode),
                           ("Answer detail", self.detail), ("Reply visibility", self.visibility),
                           ("Language (optional)", self.language)]:
            self.add_item(discord.ui.Label(text=text, component=item))
        self.default_mode = mode

    async def interaction_check(self, interaction: Any) -> bool:
        if ui.private_channel_key(interaction) != self.scope:
            await ui._ephemeral(interaction, "This request belongs to the person and channel that opened it.")
            return False
        return True

    async def on_submit(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        if self._submitted:
            await ui._ephemeral(interaction, "This request has already been submitted.")
            return
        mode = self.mode.values[0] if self.mode.values else self.default_mode
        options = [{"name": "mode", "value": mode}]
        for name, field in (("detail", self.detail), ("visibility", self.visibility)):
            value = field.values[0] if field.values else "saved"
            if value != "saved":
                options.append({"name": name, "value": value})
        if self.language.value:
            options.append({"name": "language", "value": self.language.value.strip()})
        prompt = str(self.prompt.value or "").strip() or "Work on the selected message using the chosen action."
        self._submitted = True
        request = _MessageRequestInteraction(interaction, self.target, prompt, options)
        await ui.handle_user_install_interaction(self.bot, request)

    async def on_error(self, interaction: Any, error: Exception) -> None:
        logger.warning("Message request failed (%s)", type(error).__name__)
        await ui._ephemeral(interaction, "Could not start that request. Use Ask Maxwell again to retry.")


async def _handle_message_request(bot: Any, interaction: Any) -> bool:
    if getattr(interaction, "_maxwell_modal_request", False):
        return False
    data = ui._interaction_data(interaction)
    if data.get("type") != 3 or data.get("name") not in {ui.USER_INSTALL_MESSAGE_ASK, MESSAGE_TRANSFORM}:
        return False
    target = ui.parse_target_message(interaction)
    if target is None:
        await ui._ephemeral(interaction, "Could not read the selected message. Try again on that message.")
        return True
    await interaction.response.send_modal(_MessageRequestModal(
        bot, interaction, target, mode="rewrite" if data["name"] == MESSAGE_TRANSFORM else "ask"
    ))
    return True

def install_user_install_features(bot: Any) -> None:
    """Upgrade commands and transport before Discord command sync happens."""

    del bot  # install is process-wide because user_install is a shared transport module.
    global _FEATURES_INSTALLED, _ORIGINAL_BUILD_TURN
    ui.register_interaction_handler(_handle_message_request, name="message_requests", priority=15)
    if _FEATURES_INSTALLED:
        return

    ui.USER_INSTALL_MESSAGE_EXPLAIN = MESSAGE_EXPLAIN
    ui.USER_INSTALL_MESSAGE_FACT_CHECK = MESSAGE_FACT_CHECK
    # This plugin replaces the base user-install list. Preserve discovery
    # commands registered earlier by install_usage_commands().
    from usage_commands import HELP_COMMAND, PREMIUM_COMMAND, USAGE_COMMAND

    ui.USER_INSTALL_COMMANDS[:] = [
        *modern_user_install_commands(),
        dict(HELP_COMMAND),
        dict(USAGE_COMMAND),
        dict(PREMIUM_COMMAND),
    ]
    ui.USER_INSTALL_NAMES = frozenset(
        {
            ui.USER_INSTALL_COMMAND_NAME,
            ui.USER_INSTALL_MESSAGE_ASK,
            ui.USER_INSTALL_MESSAGE_SUMMARIZE,
            ui.USER_INSTALL_USER_ASK,
            MESSAGE_EXPLAIN,
            MESSAGE_FACT_CHECK,
            MESSAGE_TRANSFORM,
            "help",
            "usage",
            "premium",
        }
    )

    _ORIGINAL_BUILD_TURN = ui.build_user_install_turn

    def build_turn(interaction: Any) -> dict[str, Any] | None:
        return _enhanced_build_turn(interaction, _ORIGINAL_BUILD_TURN)

    ui.build_user_install_turn = build_turn
    ui.snapshot_channel_history = _snapshot_channel_history
    # Payload-preserving send is the host UserInstallSession._send_impl.
    _FEATURES_INSTALLED = True


__all__ = [
    "MESSAGE_EXPLAIN",
    "MESSAGE_FACT_CHECK",
    "install_user_install_features",
    "modern_user_install_commands",
    "set_user_preference_store",
]
