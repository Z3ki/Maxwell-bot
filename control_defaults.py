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


def audio_input_enabled(owner) -> bool:
    """Whether audio bytes should reach the model.

    ``ENABLE_AUDIO_INPUT=false`` is a hard off. Dashboard ``process_audio``
    can still mute audio when the env switch is on.
    """
    cfg = getattr(owner, "config", None)
    if cfg is not None and not parse_bool(
        getattr(cfg, "ENABLE_AUDIO_INPUT", True), True
    ):
        return False
    control = getattr(owner, "_control", None) or {}
    if isinstance(control, dict) and "process_audio" in control:
        return parse_bool(control.get("process_audio"), False)
    if cfg is None:
        return False
    return parse_bool(getattr(cfg, "ENABLE_AUDIO_INPUT", False), False)


# Canonical DEFAULT_CONTROL — both bot and API import this.
# If you change a value here, it changes everywhere. That's the point.
DEFAULT_CONTROL = {
    "bot_enabled": True,
    # Off until bot_control.json turns it on. A saved true is enforced.
    "message_quota_enabled": False,
    "message_quota_limit": 300,
    "message_quota_window_seconds": 5 * 60 * 60,
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
    # ─── repetition guards ──────────────────────────────────────────────
    # Collapse repetition inside a single reply before it is sent: laugh runs
    # ("jajajajajaja" -> "ja"), doubled words, a sentence said twice, a phrase
    # repeated. Never touches fenced code. `response_guard` has done this since
    # it was written; until now nothing called it.
    "scrub_repetitions": True,
    # The other half: tell him when he has opened several messages running with
    # the same phrase. No single message is wrong, so nothing downstream can
    # catch it, and the model reads its own last reply as evidence of what it
    # sounds like and does it again.
    "self_repetition_note_enabled": True,
    "emoji_context_enabled": True,
    "music_context_enabled": True,
    "reply_dms": True,
    "reply_groups": True,
    "reply_mentions": True,
    # Direct requests must end in a visible answer, not discretionary silence.
    "require_direct_response": True,
    "respond_to_edited_mentions": True,
    "live_turn_timeout_seconds": 180,
    "inbound_retry_attempts": 2,
    "inbound_retry_delay_seconds": 5,
    # Unused for answering: startup skips the offline backlog instead of
    # replaying history. Kept so old control.json keys still sanitize.
    "gap_recovery_max_messages": 0,
    # After a mention/reply (or after Maxwell posts in a room), keep
    # watching that whole channel for about a minute so a directed
    # follow-up does not need another @. Unrelated chatter in that
    # window should stay silent. Seconds=0 also disables (legacy).
    "conversation_watch_enabled": True,
    "conversation_watch_seconds": 60,
    # Watch follow-ups wait this long for more lines, then one reply.
    # Hard @ / reply-to-Maxwell still go out immediately.
    "conversation_watch_debounce_seconds": 1,
    # How much a line nobody pinged him with has to look like it wants an
    # answer before it is worth an LLM turn (0..1, see
    # watch_policy.reply_pressure). After a ping, unrelated chatter used
    # to clear this bar on presence alone. Higher is stricter; 1.0 means
    # only hard pings.
    "conversation_watch_pressure": 0.55,
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
    # When True, the `sleep` tool and `/sleep` command can put the bot
    # into a 1-60 minute sleep window where the triggering channel gets
    # a one-shot "max is sleeping, back in Xm" notice (never a DM).
    # Default ON so the 2026-07-19 'goodnight spam' complaint has a
    # real off-switch.
    # Operators who want the bot to always be available can flip this
    # to False in dashboard.
    "enable_sleep": True,
    # ─── nightly fallback model ─────────────────────────────────────────
    # During local 22:00–09:00 hours, start requests on the configured
    # AI_FALLBACK_* endpoint/model instead of putting Maxwell to sleep.
    # If no fallback is configured, the primary provider is used normally.
    "enable_night_fallback": True,
    "night_fallback_start_hour": 22,
    "night_fallback_end_hour": 9,
    "ai_timeout_seconds": 3600,
    "memory_history_messages": 20,
    "memory_context_budget": 24000,
    "tool_history_messages": 4,
    "prompt_context_budget": 48000,
    "live_max_output_tokens": 4096,
    "max_tool_iterations": 12,
    "tool_iteration_timeout_seconds": 3600,
    "max_response_chars": 4000,
    # Prefer OpenAI-style native tool_calls when the provider supports them.
    # When this is off, the prompt teaches bare JSON lines — not XML tags.
    "native_tool_calls": True,
    "tools_enabled": True,
    # Hours a generated site lives before the cleanup loop removes it.
    # 0 = never expire. A site created with permanent=true (or extended via
    # edit_site) ignores this. Used to be a hardcoded 86400 in two places.
    "site_ttl_hours": 0,
    # Inject a restrictive CSP <meta> into every generated page. Off by
    # default: the page is the model's own document and the hosting layer is
    # where a policy belongs — the meta tag could only ever subtract from what
    # the page was written to do. Turn on if your static host sets no CSP for
    # generated sites.
    "site_inject_csp": False,
    # Unused: the full tool catalog is attached on every turn. Gating hid
    # hd_image behind more_tools and made photo requests look like a
    # from-scratch generate. Kept so existing control.json files still load.
    "lean_chat_tools": False,
    # Unused for posting: ticket greetings are configured per server via slash commands.
    # Kept so existing control.json files still load.
    "auto_ticket_greeting": False,
    "disabled_tools": [],
    "ignore_users": [],
    "allowed_channels": [],
    "blocked_channels": [],
    # {guild_id: [capability, ...]}; this is a per-server deny list. The
    # capability names are fixed in GUILD_CAPABILITIES below.
    "guild_disabled_capabilities": {},
    # {guild_id: {plugin_id: bool}}; explicit per-server plugin tool/event
    # overrides. Missing plugins inherit their global/per-user setting.
    "guild_plugin_overrides": {},
    "disabled_commands": [],
    # {guild_id: channel_id}. When a server has an entry, Maxwell only speaks
    # in that one channel there — every other channel in that server is dead to
    # him, including autonomy. Set with `/solo`, cleared with `/solo arguments:off`.
    # Scoped per server on purpose: allowed_channels is global, so using it to
    # quiet one server silences him everywhere.
    "guild_solo_channel": {},
    # Guild ids whose autonomy blacklist entry was added BY `/solo`. Only these
    # are handed back on `/solo arguments:off` — a server an admin silenced by hand stays
    # silenced.
    "guild_solo_autonomy_added": [],
    "base_personality": (
        "Natural, friendly, respectful and direct. Keep replies concise, "
        "and explain more when the user or task needs it."
    ),
    "autonomy_enabled": False,
    "autonomy_interval_seconds": 300,
    "autonomy_base_url": "",  # "" = use main provider's base_url
    "autonomy_api_key": "",  # "" = use main provider's key
    "autonomy_model": "",  # "" = use main provider's model
    "autonomy_disable_reasoning": True,  # False for endpoints that reject the reasoning param (e.g. NVIDIA)
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
    # Messages read per DM/group-DM when building the planner context. Each 100
    # is one REST round-trip, and the rendered section is char-capped anyway,
    # so reading more than this is paid for and then truncated away.
    "autonomy_dm_messages": 40,
    # Hard ceiling on the whole observe stage (all the Discord reads that build
    # planner context). Exceeding it fails the tick rather than wedging the
    # loop; gather_context logs per-phase timings so you can see what is slow.
    "autonomy_observe_timeout_seconds": 180,
}

DEAD_CONTROL_KEYS = frozenset(
    {
        # Global inference admission was retired. Persisted values must not
        # restore the old shared two-call pool after upgrading.
        "ai_concurrency",
        # Durable RAG, entity, and graph retrieval no longer run in the bot.
        "long_term_memory_enabled",
        "cross_context_enabled",
        "cross_context_max_items",
        "cross_context_min_importance",
        "entity_memory_enabled",
        "entity_memory_max_items",
        "knowledge_graph_enabled",
        "context_tier_recent_weight",
        "context_tier_ltm_weight",
        "context_tier_entity_weight",
        "context_tier_facts_weight",
        "context_tier_web_weight",

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
        # Removed memory actors and delegated workers must stay retired even
        # when an existing bot_control.json still enables them.
        'aux_api_key',
        'aux_base_url',
        'aux_disable_reasoning',
        'aux_model',
        'cross_context_dm_to_global_admin_only',
        'cross_context_extract_enabled',
        'cross_context_extract_threshold',
        'cross_context_extract_timeout_seconds',
        'entity_memory_from_extract',
        'rem_enabled',
        'rem_interval_seconds',
        'rem_max_turns',
        'rem_prompt_body',
        "bg_max_tokens",
        "bg_timeout_seconds",
        "bg_max_iters",
        "bg_context_chars",
        "autofix_enabled",
        "autofix_open_pr",
        "autofix_max_per_hour",
        "autofix_cooldown_hours",
        "context_cleanup_enabled",
        "context_cleanup_interval_seconds",
        "context_cleanup_ltm_enabled",
        # Leftovers found in live bot_control.json with zero read sites in the
        # codebase. Progress is per-server via _progress_servers now (see the
        # note in bot._load_control); the removed sub-agent knobs used to live
        # here; cross_context_budget was superseded by
        # cross_context_max_items + memory_context_budget.
        "progress_messages",
        "subagent_delegate",
        "subagent_docker",
        "subagent_max_concurrent_per_user",
        "subagent_max_timeout_minutes",
        "cross_context_budget",
        # Replaced by the nightly fallback model routing above. These keys can
        # remain in persisted control files from the old automatic sleep code,
        # but must not re-enable the removed night-sleep behavior.
        "enable_night_sleep",
        "night_sleep_start_hour",
        "night_sleep_end_hour",
        # Self-bot / unofficial X / YouTube scraping were removed.
        "x_post_enabled",
        "x_posts_per_hour",
        "x_cache_seconds",
        "x_mention_poll_seconds",
        "x_autonomy_post",
        # Retired token spend caps; message counts are the only usage measure.
        "daily_user_token_limit_enabled",
        "daily_user_token_limit",
        # Billing, plans, and mail were removed outright. A persisted value
        # must not resurrect any of them.
        "premium_billing_enabled",
        "premium_discovery_enabled",
        "message_quota_personal_plus_limit",
        "message_quota_server_plus_limit",
        "message_quota_server_fair_use_limit",
        "email_inbox_poll_seconds",
        # Voice-channel listening and speech output were removed. These keys
        # can linger in bot_control.json; strip them so they are not editable
        # and cannot turn the removed path back on.
        "vc_rms_threshold",
        "vc_pause_seconds",
        "vc_min_seconds",
        "vc_max_seconds",
        "vc_preroll_seconds",
        "vc_ai_timeout_seconds",
        "vc_ai_max_tokens",
        "vc_memory_history_messages",
        "vc_cross_context_enabled",
        "vc_max_response_chars",
        "vc_tts_engine",
        "vc_tts_voice",
        "vc_reply_mode",
        "vc_response_mode",
        "vc_wake_words",
        "vc_interrupt_enabled",
        "vc_interrupt_grace_seconds",
        "vc_debug",
        "vc_min_voiced_seconds",
        "vc_min_voiced_frames",
        "vc_max_decode_drops",
    }
)

# Fallback catalog used if plugin manifests cannot be scanned (e.g. tests
# that stub the plugins directory). The live list is discovered from
# plugins/*/plugin.json so adding a tool no longer requires editing this file.
_FALLBACK_KNOWN_TOOLS = [
    "image_generator",
    "hd_image",
    "change_presence",
    "set_activity",
    "react",
    "edit_message",
    "delete_message",
    "create_poll",
    "create_invite",
    "bot_invite_url",
    "lookup_user",
    "search_messages",
    "set_nickname",
    "forward_message",
    "typing",
    "list_servers",
    "list_admin_servers",
    "list_channels",
    "list_roles",
    "list_members",
    "leave_server",
    "create_category",
    "create_channel",
    "edit_category",
    "edit_channel",
    "move_channel",
    "clone_channel",
    "sync_channel",
    "delete_channel",
    "kick_member",
    "ban_member",
    "unban_member",
    "softban_member",
    "list_bans",
    "timeout_member",
    "list_timeouts",
    "manage_role",
    "purge_messages",
    "pin_message",
    "set_member_nickname",
    "voice_mod",
    "lock_channel",
    "lockdown",
    "set_channel_permissions",
    "list_permissions",
    "manage_invites",
    "edit_server",
    "audit_log",
    "manage_emoji",
    "change_avatar",
    "create_site",
    "edit_site",
    "delete_site",
    "site_server",
    "list_sites",
    "host_file",
    "create_thread",
    "thread_control",
    "web_search",
    "no_response",
    "shell",
    "fetch_url",
    "see_image",
    "see_video",
    "send_file",
    "send_message",
    "send_meme",
    "send_media",
    "inbox_list",
    "inbox_act",
    "sleep",
    "clear_sleep",
    "wait",
    "more_tools",
    "chess_start",
    "chess_move",
    "chess_state",
    "chess_resign",
    "manage_plugin",
    "usage",
    "debug",
    "report",
    "github_repo",
]


def _known_tools() -> list[str]:
    try:
        from maxwell_core.plugins.catalog import discover_tool_names

        discovered = discover_tool_names()
    except Exception:
        discovered = []
    seen: set[str] = set()
    out: list[str] = []
    for name in list(discovered) + list(_FALLBACK_KNOWN_TOOLS):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# Dashboard disable-list and API sanitizer. Derived from plugin manifests
# plus the fallback catalog so older installs keep every historical name.
KNOWN_TOOLS = _known_tools()


GUILD_CAPABILITIES = {
    "shell": "Linux shell",
    "web": "Web search and inspection",
    "sites": "Website hosting and editing",
    "creative": "Images and games",
    "files": "File attachments",
    "memory": "Memory tools",
    "moderation": "Moderation and server management",
    "plugins": "Optional plugins",
}

_GUILD_MODERATION_TOOLS = frozenset({
    "edit_message", "delete_message", "create_poll", "create_invite",
    "set_nickname", "create_category", "create_channel", "edit_category",
    "edit_channel", "move_channel", "clone_channel", "sync_channel",
    "delete_channel", "kick_member", "ban_member", "unban_member",
    "softban_member", "list_bans", "timeout_member", "list_timeouts",
    "manage_role", "purge_messages", "pin_message", "set_member_nickname",
    "voice_mod", "lock_channel", "lockdown", "set_channel_permissions",
    "list_permissions", "manage_invites", "edit_server", "audit_log",
    "manage_emoji", "create_thread", "thread_control",
})


def guild_capability_for_tool(name: str, *, plugin_owned: bool = False) -> str | None:
    """Map a tool to its coarse, server-configurable capability group."""
    tool = str(name or "").strip()
    if tool == "shell":
        return "shell"
    if tool in {"web_search", "fetch_url", "see_image", "see_video", "inspect_media_url"}:
        return "web"
    if tool in {"create_site", "edit_site", "delete_site", "list_sites", "host_file", "site_server"}:
        return "sites"
    if tool in {"image_generator", "hd_image", "send_meme", "chess_start", "chess_move", "chess_state", "chess_resign"} or tool.startswith("checkers_"):
        return "creative"
    if tool in {"send_file", "send_media"}:
        return "files"
    if tool in _GUILD_MODERATION_TOOLS:
        return "moderation"
    if tool.startswith(("ltm_", "memory_", "entity_", "knowledge_graph_")) or tool in {"recall_cross_server_memory", "search_messages", "context"}:
        return "memory"
    return "plugins" if plugin_owned else None
