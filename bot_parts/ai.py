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

    async def _get_autonomy_provider(self):
        """Return a provider for the autonomy loop.

        If autonomy_base_url / autonomy_model are configured, build (and cache) a
        separate OpenAICompatibleProvider. Otherwise — or on any construction/init failure —
        fall back to the main ai_provider. NEVER raise: the autonomy tick must not
        crash because of provider construction.

        Init is awaited on a fresh build (so the first tick doesn't race the
        /models probe) and re-probed whenever the cached provider is unavailable
        (so a transient init failure self-heals on a later tick instead of
        soft-skipping forever). If the dedicated provider can't initialize, the
        main ai_provider is returned for that tick so autonomy keeps running on a
        healthy endpoint; the cached provider is retained so a later tick
        re-probes and self-heals. Cache hits stay instant — the await only runs
        when construction or a re-probe is needed.
        """
        try:
            control = self._control or {}
            # Dashboard control wins; env (self.config.AUTONOMY_*) is the default
            # so a fresh install without a control.json override still routes
            # autonomy at the configured dedicated provider (e.g. NVIDIA NIM).
            base_url = (
                str(control.get("autonomy_base_url", "") or "").strip()
                or self.config.AUTONOMY_BASE_URL
            )
            api_key = (
                str(control.get("autonomy_api_key", "") or "").strip()
                or self.config.AUTONOMY_API_KEY
            )
            model = (
                str(control.get("autonomy_model", "") or "").strip()
                or self.config.AUTONOMY_MODEL
            )
            if "autonomy_disable_reasoning" in control:
                disable_reasoning = bool(
                    control.get("autonomy_disable_reasoning", True)
                )
            else:
                disable_reasoning = bool(self.config.AUTONOMY_DISABLE_REASONING)
            # No separate autonomy endpoint configured -> use main provider. This
            # also covers the both-empty case; if only a model differs (no
            # base_url) we reuse the main provider instance and pass model= per
            # request at call time.
            if not base_url:
                # base_url cleared since last tick: close the cached dedicated
                # provider (it owns an aiohttp ClientSession) so config churn
                # doesn't leak sessions, then fall through to the main provider.
                old = self.autonomy_provider
                if old is not None and hasattr(old, "close"):
                    try:
                        # Track the close task so shutdown can await/cancel it (prevents session leaks on churn).
                        task = asyncio.create_task(old.close())
                        self._track_task(task)
                    except Exception as e:
                        logger.warning(
                            f"Failed to schedule old autonomy provider close: {e}"
                        )
                self.autonomy_provider = None
                self._autonomy_provider_sig = ""
                return self.ai_provider
            sig = f"{base_url}|{api_key}|{model}|dr={_safe_int(disable_reasoning, 0)}"
            cached = (
                self.autonomy_provider if sig == self._autonomy_provider_sig else None
            )
            if cached is not None and getattr(cached, "available", False):
                return cached
            # Autonomy only generates short JSON plans — don't inherit the main
            # bot's large max_tokens, which can exceed the autonomy model's
            # output cap (e.g. minimax-m3 caps at 131072). Cap conservatively.
            autonomy_max_tokens = min(
                _safe_int(self.config.AI_MAX_OUTPUT_TOKENS or 200000, 200000), 8192
            )
            # Signature changed: close the previously cached provider (it owns an
            # aiohttp ClientSession) before replacing it, so config churn doesn't
            # leak sessions. close() is async; schedule it fire-and-forget.
            if cached is None:
                old = self.autonomy_provider
                if old is not None and hasattr(old, "close"):
                    try:
                        # Track the close task so shutdown can await/cancel it (prevents session leaks on churn).
                        task = asyncio.create_task(old.close())
                        self._track_task(task)
                    except Exception as e:
                        logger.warning(
                            f"Failed to schedule old autonomy provider close: {e}"
                        )
                provider = self._make_chat_provider(
                    name="autonomy",
                    base_url=base_url,
                    model=model or self.config.AI_MODEL,
                    max_tokens=autonomy_max_tokens,
                    api_key=api_key,
                    disable_reasoning=disable_reasoning,
                    # Inherit the main provider's fallback endpoint so a dedicated
                    # autonomy endpoint doesn't lose fallback resilience. No-op
                    # when AI_FALLBACK_* is unset (empty -> no fallback).
                    fallback_base_url=self.config.AI_FALLBACK_BASE_URL,
                    fallback_model=self.config.AI_FALLBACK_MODEL,
                    fallback_api_key=self.config.AI_FALLBACK_API_KEY,
                    fallback_disable_reasoning=self.config.AI_FALLBACK_DISABLE_REASONING,
                    fallback_reasoning_effort=getattr(
                        self.config, "AI_FALLBACK_REASONING_EFFORT", ""
                    )
                    or "",
                    retry_attempts=self.config.AI_RETRY_ATTEMPTS,
                    empty_response_retries=getattr(
                        self.config, "AI_EMPTY_RESPONSE_RETRIES", None
                    ),
                    enable_audio_input=_owner_audio_input_enabled(self),
                )
            else:
                provider = cached
            # Await init so the first tick after a build (or after a transient)
            # failure) doesn't race the /models probe. Guarded so it never raises.
            try:
                await provider.initialize()
            except Exception as e:
                logger.warning(f"Autonomy provider initialize() failed: {e}")
            self.autonomy_provider = provider
            self._autonomy_provider_sig = sig
            # If the dedicated provider couldn't initialize (primary + fallback
            # both down), fall back to the main ai_provider for this tick so
            # autonomy keeps running instead of soft-skipping forever. The cached
            # (unavailable) provider is retained so a later tick re-probes
            # initialize() and self-heals.
            if not getattr(provider, "available", False):
                logger.warning(
                    "Autonomy provider unavailable, falling back to main ai_provider for this tick"
                )
                return self.ai_provider
            return provider
        except Exception as e:
            logger.warning(f"_get_autonomy_provider failed, falling back to main: {e}")
            return self.ai_provider

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
        policy_base = str(getattr(cfg, "MAXWELL_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        policy_links = (
            f"Terms of Service: {policy_base}/terms/\nPrivacy Policy: {policy_base}/privacy/"
            if policy_base else
            "This operator has not configured policy URLs. Ask the operator for them; "
            "never substitute another deployment's policies."
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
