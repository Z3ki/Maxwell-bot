"""The prompt's memory tiers: the entity block, and the per-tier budget.

Two things are pinned here. First, that what the bot knows about a person
reaches the prompt at all, phrased so the model treats it as background about
*them* rather than as something they just said. Second, that every lookup tier
is bounded in characters — the reason for the budget is that the transcript is
assembled last, in the middle of the message list where the whole-prompt trim
cannot reach it, so an unbounded lookup tier is paid for by the running
conversation.
"""

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot, ToolCircuitBreaker


class _Memory:
    def __init__(self, entity=None, facts=(), ltm=()):
        self.entity = entity
        self.facts = list(facts)
        self.ltm = list(ltm)
        self.profile_calls = []

    async def get_channel_memory(self, channel_id):
        return []

    def get_server_prompt(self, server_id):
        return None

    def get_long_term_memory(self):
        return list(self.ltm)

    async def get_user_profile(self, user_id, query="", top_k=8, budget=None):
        self.profile_calls.append(
            {"user_id": user_id, "query": query, "top_k": top_k, "budget": budget}
        )
        facts = self.facts
        if budget is not None:
            kept, used = [], 0
            for fact in facts:
                cost = len(fact["content"])
                if kept and used + cost > budget:
                    break
                kept.append(fact)
                used += cost
            facts = kept
        out = dict(self.entity or {"user_id": user_id})
        out["facts"] = facts
        return out


def _bot(memory, control=None):
    base = {
        "base_personality": "test",
        "cross_context_enabled": False,
        "emoji_context_enabled": False,
        "long_term_memory_enabled": False,
        "entity_memory_enabled": True,
        "memory_context_budget": 30000,
        "memory_history_messages": 20,
        "music_context_enabled": False,
        "tools_enabled": False,
    }
    base.update(control or {})
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control=base,
        _drugged_until={},
        _guild_emojis={},
        _recent_users={},
        _conversation_watch={},
        _tool_system_prompt=lambda *args, **kwargs: "",
        bot_name="Maxwell",
        memory=memory,
        user=SimpleNamespace(display_name="Maxwell", id=1),
    )
    for name in (
        "_reply_parent",
        "_replying_to_own_message",
        "_render_reply_parent",
        "_reply_parent_context_lines",
        "_directly_addressed",
        "_conversation_watch_active",
        "_is_short_live_turn",
    ):
        setattr(bot, name, getattr(MaxwellBot, name).__get__(bot))
    return bot


def _message():
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="alice", id=456),
        channel=SimpleNamespace(id=123),
        guild=None,
        id=789,
        mentions=[],
        reference=None,
    )


# A turn under ~80 chars that does not address the bot reads as ambient, and
# the lookup tiers are deliberately skipped for those (see
# _is_short_live_turn). Anything testing those tiers has to be a real turn.
LONG_TURN = (
    "hey maxwell, do you remember what we agreed about the deploy schedule "
    "for the staging cluster last week? I want to double-check before friday."
)


def _prompt(bot, text=LONG_TURN):
    messages = asyncio.run(MaxwellBot._build_messages(bot, _message(), text))
    return "\n".join(str(m.get("content") or "") for m in messages)


def test_what_the_bot_knows_about_a_person_reaches_the_prompt():
    memory = _Memory(
        entity={
            "user_id": "456",
            "display_names": ["alice", "BongoCat"],
            "guild_ids": ["g1", "g2"],
            "dm_seen": True,
        },
        facts=[{"content": "works night shifts", "importance": 8}],
    )
    prompt = _prompt(_bot(memory))

    assert "About this person" in prompt
    # Phrased as global on purpose: the model is told elsewhere that the
    # transcript is this-channel-only, and without this line it treats
    # everything outside the transcript the same way.
    assert "carries across servers and DMs" in prompt
    assert "works night shifts" in prompt
    # The name on the current message is noise; the *other* names are what
    # make someone recognisable across servers.
    assert "also seen as: BongoCat" in prompt
    assert "also seen as: alice" not in prompt
    assert "2 server(s) and DMs" in prompt


def test_the_tier_is_asked_about_the_current_message():
    memory = _Memory(entity={"user_id": "456"}, facts=[{"content": "a fact"}])
    _prompt(_bot(memory), "do I still have that deploy tomorrow")

    call = memory.profile_calls[0]
    assert call["user_id"] == "456"
    assert call["query"] == "do I still have that deploy tomorrow"
    # A character budget, not just an item count — that is the whole point.
    assert call["budget"] > 0


def test_the_tier_is_bounded_in_characters():
    # A hundred long facts must not reach the prompt just because the item
    # cap allows eight of them; the budget is what actually holds.
    memory = _Memory(
        entity={"user_id": "456"},
        facts=[{"content": "x" * 5000, "importance": 5} for _ in range(100)],
    )
    bot = _bot(memory, {"entity_memory_max_items": 50})
    prompt = _prompt(bot)

    budget = memory.profile_calls[0]["budget"]
    block_size = prompt.count("x")
    assert block_size <= budget
    assert block_size < 100 * 5000


def test_switching_the_tier_off_skips_it_entirely():
    memory = _Memory(entity={"user_id": "456"}, facts=[{"content": "works nights"}])
    prompt = _prompt(_bot(memory, {"entity_memory_enabled": False}))

    assert "About this person" not in prompt
    assert "works nights" not in prompt
    # Not merely hidden — never asked for.
    assert memory.profile_calls == []


def test_the_entity_tier_follows_the_person_across_channels():
    # The global person facts are keyed on the Discord user id only, so the
    # same facts must render whether the person talks in channel A or channel B
    # (or, by the same id, DMs). This is the "remember across servers/channels"
    # guarantee — the entity tier must never be channel-scoped.
    memory = _Memory(
        entity={"user_id": "456", "display_names": ["alice"]},
        facts=[{"content": "works night shifts", "importance": 8}],
    )
    bot = _bot(memory)

    def prompt_for_channel(cid):
        msg = _message()
        msg.channel = SimpleNamespace(id=cid)
        messages = asyncio.run(MaxwellBot._build_messages(bot, msg, LONG_TURN))
        return "\n".join(str(m.get("content") or "") for m in messages)

    first = prompt_for_channel(123)
    second = prompt_for_channel(999)
    assert "works night shifts" in first
    assert "works night shifts" in second


def test_nothing_known_renders_nothing():
    # An empty "About this person:" header invites the model to invent one.
    memory = _Memory(entity={"user_id": "456", "display_names": ["alice"]}, facts=[])
    assert "About this person" not in _prompt(_bot(memory))


def test_a_memory_backend_without_the_tier_is_tolerated():
    class _Old:
        async def get_channel_memory(self, channel_id):
            return []

        def get_server_prompt(self, server_id):
            return None

    prompt = _prompt(_bot(_Old()))
    assert "About this person" not in prompt
    assert prompt  # the rest of the prompt still builds


def test_a_failing_tier_does_not_break_the_turn():
    class _Broken(_Memory):
        async def get_user_profile(self, *args, **kwargs):
            raise RuntimeError("db is on fire")

    prompt = _prompt(_bot(_Broken()))
    assert "About this person" not in prompt
    assert "You are Maxwell" in prompt or prompt


# ─── the budget plan itself ──────────────────────────────────────────────


def test_disabled_tiers_are_excluded_from_the_split():
    bot = _bot(_Memory(), {"cross_context_enabled": False, "entity_memory_enabled": False})
    plan = MaxwellBot._context_budget_plan(bot, _message(), LONG_TURN, ["prefix"])
    assert plan.budget_for("facts") == 0
    assert plan.budget_for("entity") == 0
    # Their share goes to the transcript rather than being lost.
    assert plan.budget_for("recent") > 0


def test_the_plan_never_promises_more_than_the_prompt_can_hold():
    bot = _bot(_Memory(), {"prompt_context_budget": 40000})
    plan = MaxwellBot._context_budget_plan(bot, _message(), LONG_TURN, ["x" * 5000])
    total = sum(t.budget for t in plan.tiers.values())
    assert total <= MaxwellBot._prompt_budget_chars(bot)


def test_operator_weights_move_the_split():
    bot = _bot(
        _Memory(),
        {
            "long_term_memory_enabled": True,
            "context_tier_recent_weight": 10,
            "context_tier_ltm_weight": 80,
        },
    )
    plan = MaxwellBot._context_budget_plan(bot, _message(), LONG_TURN, ["prefix"])
    assert plan.budget_for("ltm") > plan.budget_for("recent")


def test_an_ambient_turn_gives_the_lookup_tiers_budget_to_the_transcript():
    # A short line that does not address the bot skips RAG on purpose — a
    # watch/ambient turn needs the running thread, not a research pass. The
    # budget has to follow, or the transcript pays for tiers that never render.
    bot = _bot(_Memory(), {"long_term_memory_enabled": True, "cross_context_enabled": True})
    plan = MaxwellBot._context_budget_plan(bot, _message(), "lol", ["prefix"])
    assert plan.budget_for("ltm") == 0
    assert plan.budget_for("facts") == 0
    assert plan.budget_for("web") == 0
    assert plan.budget_for("recent") > 0
