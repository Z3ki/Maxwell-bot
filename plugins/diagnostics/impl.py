"""Tool implementations for the diagnostics plugin.

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

class UsageTool(Tool):
    """Query the configured usage/quota endpoint with the API key in env."""
    tool_name = 'usage'
    returns_result = True
    ends_turn = False


    def get_description(self):
        url = self._url()
        where = f" ({url})" if url else " (MAXWELL_USAGE_URL)"
        return (
            "Fetch current API usage and remaining quota from the configured "
            f"usage endpoint{where} using the API key already configured in env. "
            "Returns usage percentages, reset times, and account counts so you "
            "can report how much budget is left."
        )

    def _url(self) -> str:
        cfg = getattr(self.bot, "config", None)
        return (
            str(getattr(cfg, "MAXWELL_USAGE_URL", "") or "").strip()
            or (os.environ.get("MAXWELL_USAGE_URL", "") or "").strip()
        )

    def _api_key(self) -> str:
        return (
            os.environ.get("OLLAMA_API_KEY", "")
            or os.environ.get("OPENAI_COMPAT_API_KEY", "")
            or ""
        ).strip()

    async def execute(self, message: Message, **kwargs) -> str:
        url = self._url()
        if not url:
            return "Error: MAXWELL_USAGE_URL is not configured"
        key = self._api_key()
        if not key:
            return "Error: no API key configured (OLLAMA_API_KEY or OPENAI_COMPAT_API_KEY)."
        session = await _get_shared_session()
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return f"Error: usage endpoint returned HTTP {resp.status}: {body[:400]}"
        except asyncio.TimeoutError:
            return "Error: usage endpoint timed out."
        except Exception as exc:
            return f"Error: could not reach usage endpoint: {exc}"

        # Condense to a concise summary the model can read at a glance, with
        # the raw payload appended (truncated) only if the shape is unfamiliar.
        try:
            data = json.loads(body)
        except ValueError:
            return f"API usage from {url}:\n{body[:4000]}"

        lines: list[str] = [f"API usage from {url}:"]
        accounts = data.get("accounts")
        if accounts is not None:
            lines.append(f"Accounts: {accounts}")
        combined = data.get("combined") or {}
        if isinstance(combined, dict):
            for family, limits in combined.items():
                if not isinstance(limits, dict):
                    continue
                parts: list[str] = []
                for window in ("5h", "weekly"):
                    info = limits.get(window)
                    if not isinstance(info, dict):
                        continue
                    pct = info.get("remaining_pct")
                    reset = str(info.get("reset_time", ""))[:16]
                    name = info.get("display_name", window)
                    if pct is not None:
                        parts.append(f"{window}: {pct:.1f}% left (resets {reset})")
                    else:
                        parts.append(f"{window}: {name} (resets {reset})")
                if parts:
                    lines.append(f"- {family}: " + " · ".join(parts))
        # Antigravity pooled accounts: summarize rate-limited models WITHOUT leaking emails.
        # Previously the raw payload included per_account[].email and rate_limited[].email
        # which the LLM then echoed into the channel, exposing owner addresses.
        # We now redact emails and only show counts / anonymized summaries.
        rate_limited = data.get("rate_limited")
        # Filter to *active* limits only — antigravity-manager keeps stale entries for ~1m after expiry
        # and marks weekly 0% as rate_limited even when 5h is 100% (not actually blocked for 5h). That was
        # the "one acc always marked as rate limited" false positive (zequielwolf weekly 0% but 5h 100%).
        active_limited = []
        stale_count = 0
        if isinstance(rate_limited, list) and rate_limited:
            now_ts = int(time.time())
            for entry in rate_limited:
                if not isinstance(entry, dict):
                    continue
                until = entry.get("until")
                # until is epoch seconds; if in the past it's stale, ignore
                try:
                    until_int = int(until) if until is not None else 0
                except (ValueError, TypeError):
                    until_int = 0
                if until_int and until_int < now_ts - 5:
                    stale_count += 1
                    continue
                active_limited.append(entry)
        if active_limited:
            from collections import Counter

            models = Counter()
            for entry in active_limited:
                m = str(entry.get("model") or entry.get("reason") or "unknown")
                models[m] += 1
            summary = ", ".join(
                f"{model} x{cnt}" if cnt > 1 else model for model, cnt in models.items()
            )
            lines.append(
                f"Rate-limited models (pooled, {len(active_limited)} active): {summary}"
            )
            lines.append(
                "Note: single-model QuotaExhausted on one pooled account is NOT global exhaustion — other accounts still serve."
            )
            if stale_count:
                lines.append(
                    f"({stale_count} stale/expired rate-limit entries ignored)"
                )
        elif isinstance(rate_limited, list) and rate_limited:
            # All entries were stale/weekly-only — not actually rate limited for current window
            if stale_count:
                lines.append(
                    f"Rate-limited: none (currently) — {stale_count} stale entry expired, pooled quota still available"
                )
            else:
                lines.append("Rate-limited: none")
        else:
            lines.append("Rate-limited: none")
        # Per-account remainings are useful but must not expose emails. Anonymize to Account 1..N.
        per_account = data.get("per_account")
        if isinstance(per_account, list) and per_account:
            lines.append(
                f"Per-account pools: {len(per_account)} accounts (emails redacted)"
            )
            # Optionally show anonymized quota spread without emails
            for idx, acct in enumerate(per_account[:5], start=1):
                if not isinstance(acct, dict):
                    continue
                tier = acct.get("tier", "")
                live = acct.get("live_limited") or []
                lim_str = f" live_limited={live}" if live else ""
                # Show only remaining %s anonymized
                rem = acct.get("remaining") or {}
                parts = []
                if isinstance(rem, dict):
                    for k, v in list(rem.items())[:2]:
                        if isinstance(v, dict) and "remaining_pct" in v:
                            parts.append(f"{k}:{v['remaining_pct']:.0f}%")
                extra = " " + " ".join(parts) if parts else ""
                lines.append(f"  - Account {idx} ({tier}){lim_str}{extra}")
            if len(per_account) > 5:
                lines.append(f"  … +{len(per_account) - 5} more")

        # Build a sanitized copy for the raw payload fallback — strip every email field recursively
        def _sanitize(obj):
            if isinstance(obj, dict):
                out = {}
                for k, v in obj.items():
                    if k.lower() == "email":
                        out[k] = f"redacted_{hash(str(v)) % 10000:04d}@redacted.local"
                    else:
                        out[k] = _sanitize(v)
                return out
            if isinstance(obj, list):
                return [_sanitize(x) for x in obj]
            return obj

        sanitized = _sanitize(data)
        rendered = json.dumps(sanitized, indent=2, ensure_ascii=False)
        if len(rendered) > 2500:
            rendered = rendered[:2500] + "\n… [truncated, emails redacted]"
        lines.append("\nSanitized payload (emails redacted):" + rendered)
        return "\n".join(lines)

class ReportTool(Tool):
    """DM the configured owner with a report or error."""
    tool_name = 'report'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "DM the owner with a report. Use when something is actually broken, "
            "a user asks you to escalate, or the owner needs to know. Do not spam "
            "it for banter. Params: what (required, short summary), details "
            "(optional: who/where/what happened), kind (report|error|info, default report)."
        )

    async def execute(
        self,
        message: Message,
        what: str | None = None,
        details: str | None = None,
        kind: str = "report",
        **kwargs,
    ) -> str:
        return await notify_owner(
            self.bot,
            kind=kind,
            title=str(what or "").strip(),
            details=str(details or "").strip(),
            message=message,
        )

class DebugTool(Tool):
    """Report last-call TTFT, TPS, and token counts."""
    tool_name = 'debug'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Latency debug: last LLM call TTFT (time to first token), TPS "
            "(tokens/sec), tokens in/out, endpoint, and recent averages. "
            "No params. Use when asked how fast/slow generation is."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        return collect_debug_stats(self.bot, channel_id or None)
