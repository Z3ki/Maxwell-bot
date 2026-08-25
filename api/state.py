"""State sanitizers and domain load/save helpers for the Maxwell API server.

Reads/writes the JSON state files in data/ (bot control, REM, memory/context,
autonomy, context cleanup, commands). No route handlers, no auth logic.
Imports only from api.storage, api.config, and repo-root control_defaults — no
circular imports.
"""

import hashlib
import json
import os
import time

from api.config import (
    MAX_LTM_CHARS,
    MAX_LTM_LINES,
    REM_ENABLED_DEFAULT,
    REM_INTERVAL_DEFAULT,
)
from api.storage import (
    _autonomy_goals_path,
    _autonomy_log_path,
    _autonomy_state_path,
    _commands_path,
    _context_path,
    _control_path,
    _inbox_path,
    _load,
    _load_for_write,
    _memory_text_path,
    _rem_control_path,
    _rem_events_path,
    _rem_runs_path,
    _rem_state_path,
    _safe_float,
    _safe_int,
    _safe_list,
    _safe_object,
    atomic_json_write,
)

try:
    from control_defaults import (  # noqa: E402
        DEAD_CONTROL_KEYS,
        DEFAULT_CONTROL,
        KNOWN_TOOLS,
        parse_bool as _parse_bool,
    )
except ImportError:
    DEAD_CONTROL_KEYS = set()
    DEFAULT_CONTROL = {}
    KNOWN_TOOLS = set()

    def _parse_bool(value, default=False):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
        return default


def _sanitize_rem_control(control):
    control = _safe_object(control)
    interval = REM_INTERVAL_DEFAULT
    try:
        if control.get("interval_seconds") is not None:
            interval = max(
                10, _safe_int(control.get("interval_seconds"), REM_INTERVAL_DEFAULT)
            )
    except (TypeError, ValueError):
        pass
    max_turns = 3
    try:
        env_val = os.getenv("REM_MAX_TURNS", "3")
        max_turns = int(env_val)
    except (TypeError, ValueError):
        pass
    try:
        if control.get("max_turns") is not None:
            max_turns = max(0, min(_safe_int(control.get("max_turns"), 3), 10))
    except (TypeError, ValueError):
        pass
    return {
        "enabled": _parse_bool(control.get("enabled"), REM_ENABLED_DEFAULT),
        "interval_seconds": interval,
        "max_turns": max_turns,
        "prompt": str(control.get("prompt") or ""),
    }


def _load_rem_control():
    return _sanitize_rem_control(_load(_rem_control_path()))


def _load_rem_control_for_write():
    return _sanitize_rem_control(_load_for_write(_rem_control_path(), dict, {}))


async def _save_rem_control(control):
    await atomic_json_write(_rem_control_path(), control)


def _load_rem_status():
    control = _load_rem_control()
    state = _safe_object(_load(_rem_state_path()))
    runs = _safe_list(_load(_rem_runs_path()))
    events = _safe_list(_load(_rem_events_path()))
    last = runs[-1] if runs and isinstance(runs[-1], dict) else {}
    return {
        "enabled": control["enabled"],
        "interval_s": control["interval_seconds"],
        "last_run": state.get("last_rem_run_ts") or last.get("ts") or "",
        "events_buffered": len(events),
        "last_audit_preview": str(state.get("last_audit") or last.get("audit") or "")[
            :500
        ],
        "running": bool(state.get("running")),
    }


def _load_control():
    control = dict(DEFAULT_CONTROL)
    loaded = _safe_object(_load(_control_path()))
    control.update(loaded)
    return _sanitize_control(control)


def _sanitize_control(control):
    control = _safe_object(control)
    out = {
        k: v
        for k, v in control.items()
        if k not in DEFAULT_CONTROL and k not in DEAD_CONTROL_KEYS
    }
    out.update(DEFAULT_CONTROL)
    for key, default in DEFAULT_CONTROL.items():
        value = control.get(key, default)
        if isinstance(default, bool):
            out[key] = _parse_bool(value, default)
        elif isinstance(default, int):
            # Accept "30", 30.0 and "30.0" alike. The dashboard's number
            # inputs hand back strings, and a float-shaped one used to fall
            # silently all the way back to the default.
            try:
                out[key] = int(float(value))
            except (TypeError, ValueError):
                out[key] = default
        elif isinstance(default, float):
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                out[key] = default
        elif isinstance(default, dict):
            # {guild_id: channel_id} maps (guild_solo_channel). Ids only, so a
            # hand-edited control.json cannot smuggle anything into the gate.
            out[key] = (
                {
                    str(k).strip(): str(v).strip()
                    for k, v in value.items()
                    if str(k).strip().isdigit() and str(v).strip().isdigit()
                }
                if isinstance(value, dict)
                else {}
            )
        elif isinstance(default, list):
            if isinstance(value, list):
                items = [str(x).strip()[:64] for x in value if str(x).strip()]
                out[key] = (
                    [x for x in items if x in KNOWN_TOOLS]
                    if key == "disabled_tools"
                    else items[:500]
                )
            else:
                out[key] = []
        else:
            out[key] = value
    out["per_user_cooldown_seconds"] = max(
        0, min(out["per_user_cooldown_seconds"], 3600)
    )
    out["conversation_watch_seconds"] = max(
        0, min(_safe_int(out.get("conversation_watch_seconds"), 180), 3600)
    )
    try:
        _watch_debounce = float(
            out.get("conversation_watch_debounce_seconds")
        )
    except (TypeError, ValueError):
        _watch_debounce = 1.0
    out["conversation_watch_debounce_seconds"] = max(0.05, min(_watch_debounce, 5.0))
    # 0..1 reply-pressure bar for speaking unprompted (watch_policy).
    try:
        _watch_pressure = float(out.get("conversation_watch_pressure"))
    except (TypeError, ValueError):
        _watch_pressure = 0.4
    out["conversation_watch_pressure"] = max(0.0, min(_watch_pressure, 1.0))
    # 0 = sites never expire; otherwise a year is the ceiling.
    out["site_ttl_hours"] = max(0, min(_safe_int(out.get("site_ttl_hours"), 24), 8760))
    out["max_image_size_mb"] = max(1, min(out["max_image_size_mb"], 25))
    out["ai_timeout_seconds"] = max(10, min(out["ai_timeout_seconds"], 7200))
    out["tool_iteration_timeout_seconds"] = max(
        60,
        min(_safe_int(out.get("tool_iteration_timeout_seconds"), 3600), 14400),
    )
    out["ai_concurrency"] = max(1, min(out["ai_concurrency"], 10))
    out["email_inbox_poll_seconds"] = max(
        30, min(_safe_int(out.get("email_inbox_poll_seconds"), 120), 3600)
    )
    # 0..1 density score (watch_policy.extraction_score). Out-of-range values
    # would either extract from nothing or from every "lol" in every room.
    try:
        _extract_threshold = float(out.get("cross_context_extract_threshold"))
    except (TypeError, ValueError):
        _extract_threshold = 0.25
    out["cross_context_extract_threshold"] = max(0.0, min(_extract_threshold, 1.0))
    out["autonomy_interval_seconds"] = max(
        30, _safe_int(out.get("autonomy_interval_seconds"), 300)
    )
    out["autonomy_recent_reply_block_seconds"] = max(
        0, min(_safe_int(out.get("autonomy_recent_reply_block_seconds"), 0), 86400)
    )
    # Conversational turn-taking windows (autonomy_social.py). Clamped so a
    # fat-fingered dashboard value can't either disable turn-taking outright
    # (0 everywhere) or wedge autonomy silent for a day.
    out["autonomy_floor_cooldown_seconds"] = max(
        0,
        min(_safe_int(out.get("autonomy_floor_cooldown_seconds"), 300), 3600),
    )
    out["autonomy_floor_hold_release_seconds"] = max(
        60,
        min(
            _safe_int(out.get("autonomy_floor_hold_release_seconds"), 1800),
            86400,
        ),
    )
    out["autonomy_floor_mid_flow_seconds"] = max(
        0,
        min(_safe_int(out.get("autonomy_floor_mid_flow_seconds"), 45), 600),
    )
    out["autonomy_floor_mid_flow_messages"] = max(
        2,
        min(_safe_int(out.get("autonomy_floor_mid_flow_messages"), 2), 20),
    )
    out["autonomy_floor_idle_seconds"] = max(
        60,
        min(_safe_int(out.get("autonomy_floor_idle_seconds"), 600), 86400),
    )
    out["autonomy_base_url"] = str(out.get("autonomy_base_url", "") or "")[:512]
    out["autonomy_api_key"] = str(out.get("autonomy_api_key", "") or "")[:512]
    out["autonomy_model"] = str(out.get("autonomy_model", "") or "")[:200]
    out["aux_base_url"] = str(out.get("aux_base_url", "") or "")[:512]
    out["aux_api_key"] = str(out.get("aux_api_key", "") or "")[:512]
    out["aux_model"] = str(out.get("aux_model", "") or "")[:200]
    out["memory_history_messages"] = max(0, min(out["memory_history_messages"], 2000))
    out["memory_context_budget"] = max(1000, min(out["memory_context_budget"], 500000))
    out["tool_history_messages"] = max(
        0, min(_safe_int(out.get("tool_history_messages"), 20), 20)
    )
    out["prompt_context_budget"] = max(
        10000,
        min(_safe_int(out.get("prompt_context_budget"), 240000), 500000),
    )
    out["cross_context_max_items"] = max(
        1, min(_safe_int(out.get("cross_context_max_items"), 10), 50)
    )
    out["entity_memory_max_items"] = max(
        1, min(_safe_int(out.get("entity_memory_max_items"), 8), 50)
    )
    # Tier weights are ratios, so any non-negative value is meaningful and 0
    # is a real setting (that tier off). The upper bound exists only to keep
    # one tier from being written as 10**9 and reducing every other weight to
    # an integer-division zero.
    for _tier in ("recent", "ltm", "entity", "facts", "web"):
        _key = f"context_tier_{_tier}_weight"
        out[_key] = max(0, min(_safe_int(out.get(_key), 0), 100))
    out["cross_context_min_importance"] = max(
        1, min(_safe_int(out.get("cross_context_min_importance"), 5), 10)
    )
    out["cross_context_extract_timeout_seconds"] = max(
        5,
        min(
            _safe_int(
                out.get("cross_context_extract_timeout_seconds"), 60
            ),
            600,
        ),
    )
    out["max_tool_iterations"] = max(0, min(out["max_tool_iterations"], 100))
    out["max_response_chars"] = max(80, min(out["max_response_chars"], 8000))
    out["create_site_quota_per_user"] = max(
        0, min(_safe_int(out.get("create_site_quota_per_user"), 50), 10000)
    )
    out["autonomy_goal_stale_days"] = max(
        1, min(_safe_int(out.get("autonomy_goal_stale_days"), 14), 365)
    )
    out["autonomy_reflect_interval_seconds"] = max(
        300,
        min(
            _safe_int(out.get("autonomy_reflect_interval_seconds"), 3600),
            86400,
        ),
    )
    out["vc_rms_threshold"] = max(
        100, min(_safe_int(out.get("vc_rms_threshold"), 1200), 10000)
    )
    out["vc_pause_seconds"] = max(
        0.1, min(_safe_float(out.get("vc_pause_seconds"), 0.8), 5.0)
    )
    out["vc_min_seconds"] = max(
        0.1, min(_safe_float(out.get("vc_min_seconds"), 0.55), 10.0)
    )
    out["vc_max_seconds"] = max(
        1.0, min(_safe_float(out.get("vc_max_seconds"), 18.0), 120.0)
    )
    out["vc_preroll_seconds"] = max(
        0.0, min(_safe_float(out.get("vc_preroll_seconds"), 0.25), 3.0)
    )
    out["vc_ai_timeout_seconds"] = max(
        5, min(_safe_int(out.get("vc_ai_timeout_seconds"), 45), 180)
    )
    out["vc_ai_max_tokens"] = max(
        16, min(_safe_int(out.get("vc_ai_max_tokens"), 90), 1000)
    )
    out["vc_memory_history_messages"] = max(
        0, min(_safe_int(out.get("vc_memory_history_messages"), 2), 20)
    )
    out["vc_max_response_chars"] = max(
        40, min(_safe_int(out.get("vc_max_response_chars"), 2000), 2000)
    )
    # Free-text VC fields are enums in practice (bot.py switches on them);
    # an unknown value would silently take the else branch forever.
    _tts_engine = str(out.get("vc_tts_engine") or "fish").strip().lower()
    out["vc_tts_engine"] = (
        _tts_engine
        if _tts_engine in {"fish", "local", "espeak", "espeak-ng"}
        else "fish"
    )
    out["vc_tts_voice"] = str(out.get("vc_tts_voice") or "").strip()[:64]
    _reply_mode = str(out.get("vc_reply_mode") or "voice").strip().lower()
    out["vc_reply_mode"] = (
        _reply_mode if _reply_mode in {"voice", "text", "both"} else "voice"
    )
    _response_mode = str(out.get("vc_response_mode") or "always").strip().lower()
    out["vc_response_mode"] = (
        _response_mode if _response_mode in {"always", "addressed"} else "always"
    )
    out["vc_wake_words"] = [
        str(x).strip()[:32] for x in out.get("vc_wake_words", []) if str(x).strip()
    ][:20]
    out["base_personality"] = str(
        out.get("base_personality", DEFAULT_CONTROL["base_personality"])
    )[:12000]
    for dead_key in DEAD_CONTROL_KEYS:
        out.pop(dead_key, None)
    return out


def _normalize_memory_line(content: str) -> str:
    return " ".join(str(content).split())[:MAX_LTM_CHARS]


def _memory_lines():
    path = _memory_text_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    return [_normalize_memory_line(line) for line in lines if line.strip()][
        :MAX_LTM_LINES
    ]


def _memory_json():
    return [{"id": i + 1, "content": line} for i, line in enumerate(_memory_lines())]


def _normalize_context_content(content: str) -> str:
    return " ".join(str(content or "").split())[:1200]


def _context_entry_id(raw: dict) -> str:
    existing = str(raw.get("id") or "").strip()
    if existing:
        return existing[:32]
    stable = json.dumps(
        {
            "scope": raw.get("scope") or "global",
            "content": _normalize_context_content(raw.get("content", "")),
            "created_at": raw.get("created_at") or "",
            "source_user_id": raw.get("source_user_id") or "",
            "source_channel_id": raw.get("source_channel_id") or "",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:8]


def _sanitize_context_entries(data):
    if not isinstance(data, list):
        return []
    now = time.time()
    out = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        content = _normalize_context_content(raw.get("content", ""))
        if not content:
            continue
        expires_at = str(raw.get("expires_at") or "")
        if expires_at:
            try:
                from datetime import datetime as _dt
                from datetime import timezone as _tz

                _exp_dt = _dt.fromisoformat(expires_at[:19].replace("Z", "+00:00"))
                if _exp_dt.tzinfo is None:
                    _exp_dt = _exp_dt.replace(tzinfo=_tz.utc)
                if _exp_dt.timestamp() <= now:
                    continue
            except Exception:
                pass
        try:
            importance = int(raw.get("importance", 5))
        except (TypeError, ValueError):
            importance = 5
        visibility = str(raw.get("visibility") or "shared")[:32]
        if visibility not in {"private", "shared", "admin_only", "public_hint"}:
            visibility = "shared"
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            tags = []
        out.append(
            {
                "id": _context_entry_id(raw),
                "scope": str(raw.get("scope") or "global")[:80],
                "visibility": visibility,
                "importance": max(1, min(importance, 10)),
                "content": content,
                "source_user_id": str(raw.get("source_user_id") or "")[:64],
                "source_channel_id": str(raw.get("source_channel_id") or "")[:64],
                "source_guild_id": str(raw.get("source_guild_id") or "")[:64],
                "source_kind": str(raw.get("source_kind") or "unknown")[:32],
                "tags": [str(t).strip()[:32] for t in tags if str(t).strip()][:12],
                "created_at": str(raw.get("created_at") or "")[:64],
                "last_seen_at": str(
                    raw.get("last_seen_at") or raw.get("created_at") or ""
                )[:64],
                "expires_at": expires_at[:64],
            }
        )
    out.sort(
        key=lambda e: (e.get("last_seen_at", ""), e.get("created_at", "")), reverse=True
    )
    return out[:1000]


def _load_context_entries():
    return _sanitize_context_entries(_load(_context_path()))


def _load_context_entries_for_write():
    return _sanitize_context_entries(_load_for_write(_context_path(), list, []))


async def _save_context_entries(entries):
    await atomic_json_write(_context_path(), entries[:1000])


def _load_autonomy_state():
    return _safe_object(_load(_autonomy_state_path()))


def _load_autonomy_goals():
    data = _safe_object(_load(_autonomy_goals_path()))
    goals = data.get("goals", [])
    return goals if isinstance(goals, list) else []


def _load_autonomy_log():
    data = _safe_object(_load(_autonomy_log_path()))
    entries = data.get("entries", [])
    return entries if isinstance(entries, list) else []


def _load_inbox():
    data = _safe_object(_load(_inbox_path()))
    items = data.get("items", [])
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _load_commands():
    return _safe_list(_load(_commands_path()))


def _load_commands_for_write():
    return _load_for_write(_commands_path(), list, [])