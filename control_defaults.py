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
    # When True, the apology posted on a failed turn carries a short,
    # secret-redacted line of the ACTUAL exception (type + message) instead of
    # a bare "Sorry, please try again." Operators could only see the real cause
    # by tailing pm2 logs, which meant every user report was "it just said
    # sorry". Turn off if you don't want internals visible in a channel.
    "error_details": True,
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
    # How dense a message has to look before it is worth a context-watcher
    # call (0..1, see watch_policy.extraction_score). Lower stores more and
    # spends more; higher is stingier. This replaced a fixed list of English
    # trigger phrases, so there is nothing to keep up to date when people
    # phrase things differently.
    "cross_context_extract_threshold": 0.25,
    "emoji_context_enabled": True,
    "music_context_enabled": True,
    "reply_dms": True,
    "reply_groups": True,
    "reply_mentions": True,
    # After a mention/reply (or after Maxwell posts in a room), keep
    # watching that whole channel so a directed follow-up does not need
    # another @ or Discord reply. Default 3 minutes. 0 disables the watch.
    "conversation_watch_seconds": 180,
    # Watch follow-ups wait this long for more lines, then one reply.
    # Hard @ / reply-to-Maxwell still go out immediately.
    "conversation_watch_debounce_seconds": 1,
    # How often the background IMAP poll files new unread mail as inbox
    # notices. Only runs when ENABLE_EMAIL_TOOLS and a mailbox password are
    # set. Floor 30s, ceiling 1h.
    "email_inbox_poll_seconds": 120,
    "reply_to_bots": False,
    # Unused for starting turns. Reactions are stored on the message and
    # shown in context; they never kick off a live reply.
    "reaction_replies": False,
    "per_user_cooldown_seconds": 1.5,
    "process_images": True,
    # Audio input to the model. Off for years because the "omni" audio
    # models were not reachable; the Gemini models behind the current proxy
    # transcribe audio fine (verified on 3.7-flash and 3-pro), so this is on.
    "process_audio": True,
    "max_image_size_mb": 10,
    # When True, the `sleep` tool and `,sleep` command can put the bot
    # into a 1-60 minute sleep window where the triggering channel gets
    # a one-shot "max is sleeping, back in Xm" notice (never a DM).
    # Default ON so the 2026-07-19 'goodnight spam' complaint has a
    # real off-switch.
    # Operators who want the bot to always be available can flip this
    # to False in dashboard.
    "enable_sleep": True,
    "ai_timeout_seconds": 3600,
    "ai_concurrency": 2,
    "memory_history_messages": 60,
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
    # Hours a generated site lives before the cleanup loop removes it.
    # 0 = never expire. A site created with permanent=true (or extended via
    # edit_site) ignores this. Used to be a hardcoded 86400 in two places.
    "site_ttl_hours": 24,
    # Inject a restrictive CSP <meta> into every generated page. Off by
    # default: the page is the model's own document and the hosting layer is
    # where a policy belongs — the meta tag could only ever subtract from what
    # the page was written to do. Turn on if your static host sets no CSP for
    # generated sites.
    "site_inject_csp": False,
    # Ship a small conversational tool set on ordinary chat turns and expand to
    # the full catalog only when the turn actually asks for something (or the
    # model calls more_tools). Saves the tokens ~60 tool schemas cost on "lol".
    "lean_chat_tools": True,
    "disabled_tools": [],
    "ignore_users": [],
    "allowed_channels": [],
    "blocked_channels": [],
    "disabled_commands": [],
    # {guild_id: channel_id}. When a server has an entry, Maxwell only speaks
    # in that one channel there — every other channel in that server is dead to
    # him, including autonomy. Set with `,solo`, cleared with `,solo off`.
    # Scoped per server on purpose: allowed_channels is global, so using it to
    # quiet one server silences him everywhere.
    "guild_solo_channel": {},
    # Guild ids whose autonomy blacklist entry was added BY `,solo`. Only these
    # are handed back on `,solo off` — a server an admin silenced by hand stays
    # silenced.
    "guild_solo_autonomy_added": [],
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
    "vc_tts_engine": "fish",
    # Named Fish voice for VC replies ("tiktok", "mommy", or "" = default).
    # Maxwell can override per-reply with a leading [voice=NAME] tag.
    "vc_tts_voice": "",
    "vc_reply_mode": "voice",
    "vc_response_mode": "always",
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
    # Legacy single-purpose cooldown. Superseded by autonomy_floor_* below, which
    # subsumes it; kept because it's honored as a FLOOR on the new cooldown, so an
    # operator who tuned this up doesn't silently get a shorter window. 0 = defer
    # entirely to autonomy_floor_cooldown_seconds.
    "autonomy_recent_reply_block_seconds": 0,
    # --- Conversational turn-taking (autonomy_social.py) ---------------------
    # Autonomy runs on a timer; conversation runs on turns. These decide whether
    # Maxwell holds the floor in a room before he's allowed to speak unprompted.
    # They gate ONLY speaking — memory and goal actions are untouched.
    # Off means the planner still sees the room read but execute() stops
    # enforcing it: a debugging escape hatch, not a mode to run in.
    "autonomy_floor_enabled": True,
    # Quiet window after an *autonomy* post before another unprompted line.
    # Live replies do not start this window. Being addressed bypasses it.
    "autonomy_floor_cooldown_seconds": 300,
    # How long he keeps holding the floor after speaking into silence. Past this
    # the room has plainly moved on and starting something fresh is fair.
    "autonomy_floor_hold_release_seconds": 1800,
    # Several messages from several people inside this window = an exchange in
    # progress; cutting in is what makes a bot feel like an interruption.
    "autonomy_floor_mid_flow_seconds": 45,
    "autonomy_floor_mid_flow_messages": 2,
    # Silence past this and the room reads as idle rather than active.
    "autonomy_floor_idle_seconds": 600,
    # Autonomy-specific blacklists (separate from general blocked_channels/allowed_channels).
    # These prevent autonomy from posting/DMing or acting in listed channels or servers (guilds),
    # while normal bot replies (mentions etc) can still work if not otherwise blocked.
    "autonomy_blocked_channels": [],
    "autonomy_blocked_servers": [],
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
        "autonomy_drives_enabled",
        # The context janitor was replaced by RAG memory. bot.py answers every
        # context_cleanup_* command with "engine removed" and the API routes are
        # no-op stubs, so keeping these in DEFAULT_CONTROL only put three
        # switches in the dashboard that could not do anything.
        "context_cleanup_enabled",
        "context_cleanup_interval_seconds",
        "context_cleanup_ltm_enabled",
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
    "server_setup",
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
    "change_avatar",
    "create_site",
    "edit_site",
    "delete_site",
    "site_server",
    "list_sites",
    "web_search",
    "no_response",
    "shell",
    "fetch_url",
    "see_image",
    "see_video",
    "youtube",
    "send_file",
    "send_message",
    "send_meme",
    "send_media",
    "inbox_list",
    "inbox_act",
    "join_vc",
    "vc_status",
    "vc_where",
    "leave_vc",
    "sleep",
    "clear_sleep",
    "wait",
    "update_base_personality",
    "update_server_prompt",
    "email_send",
    "email_read_inbox",
    "email_get_message",
    "email_search",
    "more_tools",
]
