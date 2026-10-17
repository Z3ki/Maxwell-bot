"""AI provider selection, night-fallback, generation and AI-slot limiting.

Split out of bot.py; methods keep their original bodies.
"""

import bot_common as _common

# Mechanical split: the methods below were written against bot.py's
# module globals. Bind every shared name so the bodies run unchanged.
for _name in dir(_common):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_common, _name))
del _name

class AIMixin:

    def _is_in_night_fallback_window(self) -> bool:
        """Return whether local time is inside the nightly fallback window."""
        try:
            control = getattr(self, "_control", None) or {}
            if not parse_bool(control.get("enable_night_fallback"), True):
                return False
            start = max(
                0, min(23, _safe_int(control.get("night_fallback_start_hour"), 22))
            )
            end = max(0, min(23, _safe_int(control.get("night_fallback_end_hour"), 9)))
            hour = time.localtime().tm_hour
            if start == end:
                return False
            if start < end:
                return start <= hour < end
            return hour >= start or hour < end
        except Exception:
            return False

    def _night_fallback_active(self) -> bool:
        """Return whether the configured fallback should serve this request."""
        if not self._is_in_night_fallback_window():
            return False
        config = getattr(self, "config", None)
        return bool(
            str(getattr(config, "AI_FALLBACK_BASE_URL", "") or "").strip()
            and str(getattr(config, "AI_FALLBACK_MODEL", "") or "").strip()
        )

    def _night_fallback_kwargs(self, provider=None) -> dict[str, bool]:
        """Return provider kwargs for the main provider's night routing."""
        main_provider = getattr(self, "ai_provider", None)
        if provider is not None and provider is not main_provider:
            return {}
        return {"prefer_fallback": True} if self._night_fallback_active() else {}

    async def _generate_response(self, messages: list[dict], **kwargs):
        """Generate through the main provider, preferring fallback at night."""
        provider = kwargs.pop("provider", None)
        quota_user_id = kwargs.pop("quota_user_id", None)
        charge_message = bool(kwargs.pop("charge_message", False))
        selected_provider = provider or self.ai_provider
        from provider_limits import request_timeout_seconds
        kwargs.setdefault("timeout", request_timeout_seconds(self, selected_provider))
        if provider is None:
            for key, value in self._night_fallback_kwargs().items():
                kwargs.setdefault(key, value)
        else:
            # User-supplied provider calls are isolated and never inherit
            # Maxwell's fallback endpoints or routing preferences.
            kwargs.pop("prefer_fallback", None)
        message = _current_inbound.get()
        if message is not None:
            kwargs.setdefault("request_id", str(getattr(message, "id", "") or ""))
        author = getattr(message, "author", None)
        if quota_user_id is None and author is not None and not getattr(author, "bot", False):
            quota_user_id = getattr(author, "id", None)
        if (
            charge_message
            and quota_user_id is not None
            and self._control.get("message_quota_enabled", False)
        ):
            quotas = getattr(self, "_message_quota", None)
            if quotas is not None:
                guild = getattr(message, "guild", None)
                quotas.charge(
                    str(quota_user_id),
                    enforced_message_limit(self._control),
                    enforced_window_seconds(self._control),
                    guild_id=str(getattr(guild, "id", "") or ""),
                )
        if provider is None and not kwargs.get("model"):
            premium_model = str(
                getattr(getattr(self, "config", None), "AI_PREMIUM_MODEL", "") or ""
            ).strip()
            if premium_model and self._wants_premium_model(str(quota_user_id or ""), message):
                kwargs["model"] = premium_model
        # This step is working again: any typing that a previous send
        # suspended in this channel comes back only now, so the indicator
        # never trails the last delivered message.
        await typing_resume(self, getattr(message, "channel", None))
        started = time.monotonic()
        try:
            return await selected_provider.generate_response(messages, **kwargs)
        finally:
            if message is not None:
                logger.info(
                    "inbound mid=%s cid=%s stage=provider duration_ms=%d requested_model=%s",
                    getattr(message, "id", ""),
                    getattr(getattr(message, "channel", None), "id", ""),
                    (time.monotonic() - started) * 1000,
                    kwargs.get("model") or getattr(selected_provider, "model", ""),
                )


    def _get_personality(self) -> str:
        """Return Maxwell's code-owned shared personality."""
        base = str(DEFAULT_CONTROL["base_personality"])
        cfg = getattr(self, "config", None)
        values = getattr(self, "_identity", None)
        if not values:
            values = identity_values(cfg)
        if os.getenv("BOT_BIRTHDAY", "").strip():
            birthday = parse_birthday(getattr(cfg, "BOT_BIRTHDAY", None) if cfg else None)
            self._BIRTHDAY = birthday
            age_days = (datetime.now(timezone.utc) - birthday).days
            born = values.get("birthday_long") or (
                f"{birthday.strftime('%B')} {birthday.day}, {birthday.year}"
            )
            age_line = (
                f"\nYou are currently {age_days} days old. You were born on {born}."
            )
            if "You are currently" not in base:
                base += age_line
            else:
                base = re.sub(r"\nYou are currently \d+ days old\..*", age_line, base)
        prompts = getattr(self, "prompts", None)
        if prompts is not None:
            from maxwell_core.prompts.component import PromptRequest

            # Only code-owned core components may extend the shared persona.
            # Plugin instructions belong to their task/tool prompt, never here.
            base = prompts.personality_text(
                PromptRequest(scope="discord"),
                enabled_plugins=("core",),
                base=base,
            )
        from privacy import policy_urls
        urls = policy_urls(cfg)
        policy_links = "\n".join(f"{name.title()}: {url}" for name, url in urls.items()) if urls else (
            "This operator has not configured policy URLs. Ask the operator for them; never substitute another deployment's policies."
        )
        return fill_identity(base, values) + (
            "\n\n## This deployment's policies\n"
            + policy_links
            + "\nProvide these links when someone asks about terms or privacy. "
            "Do not proactively announce policies or send first-use legal DMs. "
            "Refer to the current pages for policy details; do not invent their contents "
            "or claim a user has received or accepted a notice."
        )

    async def _personal_provider(self, message):
        """Saved personal model for this sender, including normal chat.

        App commands and mentions share this path. Someone without a saved key
        stays on Maxwell's default model. A saved key that cannot be read fails
        the turn instead of quietly spending the operator's provider.
        """
        author = getattr(message, "author", None)
        if getattr(author, "bot", False):
            return None
        vault = getattr(self, "_byok_vault", None)
        author_id = str(getattr(author, "id", "") or "")
        if vault is None or not author_id.isdigit():
            return None
        try:
            has_credential = await asyncio.to_thread(vault.has_credential, author_id)
            if not has_credential:
                return None
            credential = await asyncio.to_thread(vault.get, author_id)
            if not credential:
                raise RuntimeError("saved personal provider is unavailable")
            from plugins.maxwell_extras.byok import make_request_provider

            return make_request_provider(self, credential)
        except Exception as exc:
            logger.warning(
                "Personal provider setup failed for requester %s (%s)",
                author_id,
                type(exc).__name__,
            )
            raise RuntimeError(
                "saved personal provider configuration is unavailable"
            ) from None

    async def _acquire_ai_slot(
        self, timeout: float, *, priority: str = "background", key: str = ""
    ):
        """Track an active inference call; all calls start without admission waits.

        Keep the call signature shared with optional conversational autonomy.
        The provider and live-turn deadlines still bound execution time.
        """
        task = asyncio.current_task()
        if task is not None:
            self._ai_call_kind[task] = priority

    async def _release_ai_slot(self):
        task = asyncio.current_task()
        if task is not None:
            self._ai_call_kind.pop(task, None)

    def ai_slot_stats(self) -> dict:
        """Active inference calls; zero capacity denotes unlimited admission."""
        return {
            "capacity": 0,
            "active": len(self._ai_call_kind),
            "waiting": 0,
            "waiting_by_priority": {},
            "known_keys": 0,
        }
