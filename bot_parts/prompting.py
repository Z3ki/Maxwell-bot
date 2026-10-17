"""Prompt assembly: budgets, entity blocks, thread context and repetition checks.

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

class PromptingMixin:

    def _thread_prompt_block(self, message) -> str:
        """Brief injected when this turn is inside a Discord thread."""
        store = getattr(self, "thread_store", None)
        if store is None:
            return ""
        try:
            return store.prompt_block(message)
        except Exception:
            logger.debug("thread prompt block skipped", exc_info=True)
            return ""

    @staticmethod
    def _topic_tokens(text: str) -> set[str]:
        stop = {
            "the",
            "and",
            "for",
            "you",
            "that",
            "this",
            "with",
            "what",
            "when",
            "where",
            "why",
            "how",
            "are",
            "was",
            "were",
            "from",
            "have",
            "has",
            "had",
            "not",
            "but",
            "just",
            "like",
            "about",
        }
        return {
            t
            for t in re.findall(r"[a-z0-9_]{4,}", str(text or "").lower())
            if t not in stop
        }

    @classmethod
    def _shared_fact_relevant(cls, latest: str, fact: dict) -> bool:
        scope = str(fact.get("scope") or "")
        if scope.startswith(("user:", "channel:", "dm:")):
            return True
        latest_tokens = cls._topic_tokens(latest)
        # Short vague turns like "lol" should not drag in guild/global lore.
        if len(latest_tokens) < 2:
            return False
        fact_text = (
            str(fact.get("content") or "") + " " + " ".join(fact.get("tags") or [])
        )
        return bool(latest_tokens & cls._topic_tokens(fact_text))

    @staticmethod
    def _message_content_chars(message: dict) -> int:
        """Prompt size of one message — see tool_schemas.message_chars."""
        return message_chars(message)

    @staticmethod
    def _trim_middle(text: str, limit: int) -> str:
        text = str(text or "")
        if len(text) <= limit:
            return text
        if limit <= 200:
            return text[:limit]
        keep = max(80, (limit - 80) // 2)
        omitted = len(text) - (keep * 2)
        return (
            text[:keep]
            + f"\n\n[... prompt budget trimmed {omitted} chars ...]\n\n"
            + text[-keep:]
        )

    def _prompt_budget_chars(self) -> int:
        """Chars the whole prompt may occupy, output headroom already removed.

        2026-07-19: model context window is 256k. Do not spend it. A 60k–90k
        char prompt (plus the tools= payload) is plenty for Discord chat; the
        previous default of 240k chars plus a full 80-tool catalog burned
        quota on "wyd". Operators can still raise the control if they want
        the whole window. The output reserve scales with the budget so a
        full context window can't leave the model with no room to answer.
        """
        raw_budget = max(
            10000,
            min(
                _safe_int(
                    self._control.get("prompt_context_budget", 96000) or 96000,
                    240000,
                ),
                2000000,
            ),
        )
        output_reserve = max(16000, raw_budget // 4)
        provider = _current_request_provider.get()
        window = getattr(getattr(provider, "capabilities", None), "context_window", None)
        if isinstance(window, int) and window > 0:
            raw_budget = min(raw_budget, max(4000, window * 4))
            output_reserve = min(output_reserve, max(1000, raw_budget // 4))
            return max(2000, raw_budget - output_reserve)
        return max(10000, raw_budget - output_reserve)

    _ECHO_OPENER_WORDS = 4
    _ECHO_LOOKBACK = 8
    _ECHO_MIN_REPEATS = 3

    def _self_repetition_note(self, memory: list[dict] | None) -> str:
        """Tell him when he has been opening every message the same way.

        Repetition inside one message is cleaned up after generation
        (`_sanitize_visible_reply`). This is the other half: the same catchphrase
        opening six messages running. No single message is wrong, so nothing
        downstream can catch it, and the model does not notice the pattern in its
        own transcript — it reads its last reply as evidence of what it sounds
        like and does it again, harder.

        Only fires on an actual streak, and names the phrase rather than giving
        general advice, because "vary your language" changes nothing and "you
        have started 4 of your last 8 messages with 'jajaja'" does.
        """
        if not memory:
            return ""
        control = getattr(self, "_control", None) or {}
        if not control.get("self_repetition_note_enabled", True):
            return ""
        self_id = (
            str(getattr(self.user, "id", "")) if getattr(self, "user", None) else ""
        )
        openers: list[str] = []
        for entry in reversed(memory):
            if len(openers) >= MaxwellBot._ECHO_LOOKBACK:
                break
            if entry.get("is_tool"):
                continue
            is_self = bool(entry.get("author_is_bot")) or (
                self_id and str(entry.get("author_id") or "") == self_id
            )
            if not is_self:
                continue
            words = re.findall(
                r"[\w\u00C0-\u024F']+", str(entry.get("content") or "").lower()
            )
            if not words:
                continue
            openers.append(" ".join(words[: MaxwellBot._ECHO_OPENER_WORDS]))
        if len(openers) < MaxwellBot._ECHO_MIN_REPEATS:
            return ""

        # Compare on the first word or two: "jajaja bro" and "jajajaja man" are
        # the same tic, and an exact-match check would miss both.
        def _stem(opener: str) -> str:
            first = opener.split(" ", 1)[0]
            # Any laugh run normalizes to one token so length does not hide it.
            return re.sub(r"(?i)^((?:j[aeiou]|h(?:a|e)|lo)+)$", r"<laugh>", first)

        counts: dict[str, list[str]] = {}
        for opener in openers:
            counts.setdefault(_stem(opener), []).append(opener)
        stem, hits = max(counts.items(), key=lambda kv: len(kv[1]))
        if len(hits) < MaxwellBot._ECHO_MIN_REPEATS:
            return ""
        phrase = hits[0].split(" ", 1)[0]
        return (
            f"You have opened {len(hits)} of your last {len(openers)} messages with "
            f'"{phrase}". It has stopped meaning anything. Do not open this one '
            "that way — say the thing itself, or say nothing."
        )

    # ─── per-tier context budget ──────────────────────────────────────

    def _context_budget_plan(
        self, message, user_message: str, system_parts: list[str]
    ) -> BudgetPlan:
        """Give the remaining prompt budget to scoped conversation history."""
        overhead = sum(len(part) for part in system_parts) + 4000
        total = max(0, MaxwellBot._prompt_budget_chars(self) - overhead)
        return allocate(total, disabled={"ltm", "web", "facts", "entity"})

    async def _entity_profile_for(
        self, message, user_message: str, budget: int
    ) -> tuple[dict | None, list[dict]]:
        """Return identity facts that are authorized in this message's scope.

        Entity fact retrieval applies the same guild, channel, user, and DM
        boundaries as the other memory tiers. A Discord user ID alone does not
        authorize sharing a fact across servers.
        """
        author = getattr(message, "author", None)
        uid = str(getattr(author, "id", "") or "")
        if not uid or budget <= 0:
            return None, []
        get_profile = getattr(self.memory, "get_user_profile", None)
        if not callable(get_profile):
            return None, []
        control = getattr(self, "_control", None) or {}
        max_items = max(
            1,
            min(_safe_int(control.get("entity_memory_max_items", 8) or 8, 8), 50),
        )
        profile = await get_profile(
            uid, query=user_message, top_k=max_items, budget=budget,
            requester=_memory_requester_for(self, message),
        )
        return profile, list(profile.get("facts") or [])

    def _render_entity_block(
        self, message, entity: dict | None, facts: list[dict]
    ) -> str:
        """Render the entity tier, or "" when there is nothing worth saying.

        Aliases are only shown when they differ from the name on the current
        message — "also known as: alice" under a message from alice is pure
        noise, and it is the *other* names that make someone recognisable
        across servers.
        """
        if not entity:
            return ""
        author = getattr(message, "author", None)
        current = str(getattr(author, "display_name", "") or "").strip()
        lines: list[str] = []
        aliases = [
            str(n)
            for n in (entity.get("display_names") or [])
            if str(n).strip() and str(n).strip() != current
        ][:4]
        if aliases:
            lines.append("- also seen as: " + ", ".join(aliases))
        guild_count = len(entity.get("guild_ids") or [])
        if guild_count > 1 or (guild_count and entity.get("dm_seen")):
            where = f"{guild_count} server(s)" if guild_count else ""
            if entity.get("dm_seen"):
                where = (where + " and DMs") if where else "DMs"
            lines.append(f"- known from: {where}")
        for fact in facts:
            content = str(fact.get("content") or "").strip()
            if content:
                lines.append(f"- {content}")
        if not lines:
            return ""
        return (
            "About this person (authorized reference facts; background, don't recite):\n"
            + "\n".join(lines)
        )

    def _apply_prompt_budget(self, messages: list[dict]) -> list[dict]:
        budget = MaxwellBot._prompt_budget_chars(self)
        total = sum(MaxwellBot._message_content_chars(m) for m in messages)
        if total <= budget:
            return messages
        out = [dict(m) for m in messages]
        # Only explicitly delimited historical reference may be shed. System
        # blocks carry persona, permissions, trust boundaries and turn rules;
        # arbitrary middle clipping can invert them. Live requests and native
        # tool calls/results are indivisible here (trim_tool_tail owns pairing).
        live_index = max(
            (idx for idx, entry in enumerate(out) if entry.get("role") == "user"),
            default=-1,
        )
        dropped: set[int] = set()
        # Optional older recall is shed before the recent conversational thread.
        for opener, closer in (
            ("<recalled_conversation>\n", "\n</recalled_conversation>"),
            ("<previous_conversation>\n", "\n</previous_conversation>"),
        ):
            for idx, entry in enumerate(out):
                if total <= budget:
                    break
                old = entry.get("content")
                if (
                    entry.get("role") != "user"
                    or idx == live_index
                    or entry.get("tool_calls")
                    or entry.get("tool_call_id")
                    or not isinstance(old, str)
                    or not old.startswith(opener)
                    or not old.endswith(closer)
                ):
                    continue
                # Preserve whole historical blocks and their author/provenance.
                dropped.add(idx)
                total -= len(old)
        out = [entry for idx, entry in enumerate(out) if idx not in dropped]
        if total > budget:
            logger.warning(
                "Prompt exceeds soft budget: %s/%s chars; preserving instructions, live input and tool records",
                total, budget,
            )
        else:
            logger.info("Trimmed historical prompt to %s/%s chars", total, budget)
        return out

    async def _build_messages(
        self, message, user_message: str,
        has_media: bool = False, media_summary: str = "",
    ) -> list[dict]:
        """Delegate prompt construction to the transport-independent service."""
        hooks = ConversationPromptHooks(
            apply_prompt_budget=MaxwellBot._apply_prompt_budget,
            context_budget_plan=MaxwellBot._context_budget_plan,
            entity_profile_for=MaxwellBot._entity_profile_for,
            graph_prompt_block=MaxwellBot._graph_prompt_block,
            message_content_chars=MaxwellBot._message_content_chars,
            prompt_budget_chars=MaxwellBot._prompt_budget_chars,
            render_entity_block=MaxwellBot._render_entity_block,
            self_repetition_note=MaxwellBot._self_repetition_note,
            thread_prompt_block=MaxwellBot._thread_prompt_block,
            trim_middle=MaxwellBot._trim_middle,
        )
        return await ConversationPromptBuilder(self, hooks).build(
            message, user_message, has_media, media_summary,
        )
