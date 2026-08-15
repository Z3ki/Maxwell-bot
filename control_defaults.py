"""Shared control defaults for Maxwell Bot.

Single source of truth for DEFAULT_CONTROL, KNOWN_TOOLS, and parse_bool.
Both bot.py and api_server.py import from here so config ranges never drift.
"""


def parse_bool(value, default: bool = False) -> bool:
    """Parse persisted/env booleans. bool("false") is True because Python is an asshole."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


# Canonical DEFAULT_CONTROL — both bot and API import this.
# If you change a value here, it changes everywhere. That's the point.
DEFAULT_CONTROL = {
    "bot_enabled": True,
    "log_messages": False,
    "error_replies": True,
    "typing_indicator": True,
    "store_memory": True,
    "long_term_memory_enabled": True,
    "cross_context_enabled": True,
    "cross_context_extract_enabled": True,
    "cross_context_max_items": 10,
    "cross_context_min_importance": 5,
    "cross_context_dm_to_global_admin_only": True,
    # Per-call timeout for the background context-extraction LLM call
    # (the one that asks the model to summarize a message into a
    # durable shared-context fact). 20s was way too tight for cold-start
    # 1M-context models — the call would time out, retry, fall back to
    # a smaller model, and flood the provider log. 60s is generous enough
    # for a cold start and still short enough that one stuck call can't
    # back up the rest of the context-extract queue.
    "cross_context_extract_timeout_seconds": 60,
    "emoji_context_enabled": True,
    "music_context_enabled": True,
    "reply_dms": False,
    "reply_groups": False,
    "reply_mentions": True,
    "reply_to_bots": False,
    # When False, reactions on the bot's own messages are swallowed
    # silently (logged + deduped) — no fake_message, no LLM turn, and
    # crucially no 'XYZ reacted to your message with …' status posted in
    # the channel. Reactions are not text and shouldn't kick off a
    # generation. Default off so channel reactions don't spam noise,
    # opt-in via dashboard control if you want emoji-aware replies.
    "reaction_replies": False,
    "per_user_cooldown_seconds": 1.5,
    "process_images": True,
    "process_audio": False,
    "max_image_size_mb": 10,
    # When True, the `sleep` tool and `,sleep` command can put the bot
    # into a 1-60 minute sleep window where incoming pings/DMs get a
    # one-shot "max is sleeping, back in Xm" notification. Default ON
    # so the 2026-07-19 'goodnight spam' complaint has a real off-switch.
    # Operators who want the bot to always be available can flip this
    # to False in dashboard.
    "enable_sleep": True,
    "ai_timeout_seconds": 3600,
    "ai_concurrency": 2,
    "memory_history_messages": 30,
    "memory_context_budget": 200000,
    "tool_history_messages": 20,
    "prompt_context_budget": 240000,
    "max_tool_iterations": 30,
    "tool_iteration_timeout_seconds": 3600,
    "max_response_chars": 4000,
    # Prefer OpenAI-style native tool_calls when the provider supports them.
    # XML text tags remain as a fallback when the model emits tags without
    # native tool_calls (or the endpoint rejects tools=).
    "native_tool_calls": True,
    "tools_enabled": False,
    "create_site_quota_per_user": 50,
    "disabled_tools": [],
    "ignore_users": [],
    "allowed_channels": [],
    "blocked_channels": [],
    "disabled_commands": [],
    "base_personality": (
        "You are Maxwell — a person in the chat, not a service. Use your own "
        "judgment about what to say, how to say it, and whether to engage. "
        "Let the conversation set tone, energy, directness, and length. "
        "Swearing is fine when it fits. "
        "When someone asks you to make something concrete, call the matching "
        "tool in the same turn — describing an artifact without producing it "
        "is a hallucination. "
        "Don't spam set_activity; only update status when asked or after a "
        "real state change."
    ),
    "vc_rms_threshold": 1200,
    "vc_pause_seconds": 0.8,
    "vc_min_seconds": 0.55,
    "vc_max_seconds": 18,
    "vc_preroll_seconds": 0.25,
    "vc_ai_timeout_seconds": 45,
    "vc_ai_max_tokens": 1000,
    "vc_memory_history_messages": 2,
    "vc_cross_context_enabled": False,
    "vc_max_response_chars": 2000,
    "vc_tts_engine": "local",
    "vc_reply_mode": "voice",
    "vc_response_mode": "addressed",
    "vc_wake_words": ["maxwell"],
    "vc_interrupt_enabled": True,
    "vc_debug": True,
    "autonomy_enabled": False,
    "autonomy_interval_seconds": 300,
    "autonomy_base_url": "",  # "" = use main provider's base_url
    "autonomy_api_key": "",  # "" = use main provider's key
    "autonomy_model": "",  # "" = use main provider's model
    "autonomy_disable_reasoning": True,  # False for endpoints that reject the reasoning param (e.g. NVIDIA)
    # Auxiliary background agents (REM, context-cleanup, context-watcher).
    # "" = fall back to the autonomy config, then the main provider, so a
    # control.json without aux overrides keeps the old shared-endpoint
    # behaviour. Set these to route the context-manager brains to a
    # separate model/endpoint from the autonomy tick loop.
    "aux_base_url": "",  # "" = use autonomy (then main) base_url
    "aux_api_key": "",  # "" = use autonomy (then main) key
    "aux_model": "",  # "" = use autonomy (then main) model
    "aux_disable_reasoning": True,  # False for endpoints that reject the reasoning param
    "autonomy_min_post_gap_seconds": 0,  # deprecated — no longer enforced, kept for compat
    "autonomy_recent_reply_block_seconds": 0,  # skip autonomy post if bot replied in-channel within this window (0=off)
    "context_cleanup_enabled": True,  # background context janitor (dedupe/merge/remove weird shared-context facts)
    "context_cleanup_interval_seconds": 1800,  # how often the janitor runs (>=300s)
    "context_cleanup_ltm_enabled": True,  # also clean long_term_memory (where remote feeds/intel would have dumped)
    # Autonomy-specific blacklists (separate from general blocked_channels/allowed_channels).
    # These prevent autonomy from posting/DMing or acting in listed channels or servers (guilds),
    # while normal bot replies (mentions etc) can still work if not otherwise blocked.
    "autonomy_blocked_channels": [],
    "autonomy_blocked_servers": [],
    # Self-directed agency: internal "drives" (curiosity/social/creative/reflective/restless)
    # that evolve each tick and bias what Maxwell feels like doing, plus an idle-initiative
    # hint that permits acting on your own when nothing external needs you. Lets the bot
    # do what it wants, whenever, without a human triggering it.
    "autonomy_drives_enabled": True,
    # Goals not acted on for this many days are flagged STALE in context (candidates for
    # the complete_goal action). Not auto-deleted — the bot decides to retire them.
    "autonomy_goal_stale_days": 14,
    # Periodic reflection nudge injected into context roughly every N seconds so Maxwell
    # self-reviews goals/memory and sets new objectives on its own cadence.
    "autonomy_reflect_enabled": True,
    "autonomy_reflect_interval_seconds": 3600,
}

DEAD_CONTROL_KEYS = frozenset(
    {
        "auto_mode_enabled",
        "auto_eval_every",
        "auto_max_recent_replies",
        "auto_recent_window_minutes",
        "auto_inactivity_minutes",
        "auto_decider_prompt",
        # Intel engine was removed in d455e4b. These keys can linger in
        # persisted bot_control.json from older installs; strip them so
        # the dashboard's stale-key warning list stays clean.
        "intel_enabled",
        "intel_interval_seconds",
        "intel_feed_urls",
        "intel_run_history",
    }
)

# Keep in sync with bot._setup_tools(). Only LLM-facing tools; no command-queue types.
KNOWN_TOOLS = [
    "image_generator",
    "hd_image",
    "change_presence",
    "set_activity",
    "react",
    "edit_message",
    "delete_message",
    "create_poll",
    "create_invite",
    "lookup_user",
    "search_messages",
    "set_nickname",
    "forward_message",
    "typing",
    "tts",
    "list_servers",
    "list_admin_servers",
    "create_category",
    "create_channel",
    "edit_channel",
    "delete_channel",
    "change_avatar",
    "create_site",
    "list_sites",
    "web_search",
    "no_response",
    "shell",
    "fetch_url",
    "youtube",
    "send_file",
    "send_message",
    "send_meme",
    "send_media",
    "leave_vc",
    "sleep",
    "clear_sleep",
]
