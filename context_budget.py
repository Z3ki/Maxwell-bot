"""Per-tier context budget allocation.

The prompt is assembled from several independent memory tiers — the channel
transcript, semantically-recalled long-term facts, what we know about the
person talking, cross-context facts, and cached web results. Before this
module each tier was capped by an *item count* (``long_term_memory_max_items``,
``cross_context_max_items``, …) and only the transcript had a character
budget. Item counts are a terrible proxy for size: fifty one-line facts and
fifty paragraph-long ones differ by two orders of magnitude, so the real
prompt size swung wildly and the transcript — trimmed last, in the middle of
the message list where ``_apply_prompt_budget`` cannot reach it — absorbed
every overshoot.

What this does instead: take the characters actually available for memory,
split them across tiers by weight, and hand each tier a hard character
budget it must fit inside. Tiers that come in under budget give their
leftovers back to the tiers that want more, so a quiet channel with a short
transcript spends its slack on facts instead of wasting it.

Chars, not tokens
-----------------
Everything here counts characters. The bot's existing budget plumbing
(``prompt_context_budget``, ``_prompt_budget_chars``) is char-based, and a
char budget is provider-independent — no tokenizer to load, no per-model
drift. ``CHARS_PER_TOKEN`` is the conversion the rest of the codebase
assumes; it is exposed for callers that need to report in tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The ratio the bot's own budget math already assumes. English prose sits
# near 4; code and IDs sit lower. Only used for reporting.
CHARS_PER_TOKEN = 4

# Tier order is the order they are filled and, on a tie, the order in which
# leftover characters are offered. Transcript first: losing the running
# conversation is the most visible failure, so it gets first refusal on
# slack. `web` is last because a stale cached page is the most disposable
# thing in the prompt.
TIER_ORDER = ("recent", "ltm", "entity", "facts", "web")

# Default share of the memory budget per tier, as weights (not percentages —
# they are normalized, so they do not have to sum to anything in particular).
# The transcript dominates deliberately: it is the only tier that carries
# conversational state, and the others are lookups that stay useful when
# thin.
DEFAULT_WEIGHTS: dict[str, int] = {
    "recent": 70,
    "ltm": 12,
    "entity": 8,
    "facts": 7,
    "web": 3,
}

# Floor per tier, in chars. A tier that gets less than this is better off
# with nothing — half a fact is noise, and a two-line transcript is worse
# than no transcript because it reads as if that is all that was said.
# Floors are only honored while the total budget can pay for them; see
# `allocate`.
DEFAULT_MINIMUMS: dict[str, int] = {
    "recent": 2000,
    "ltm": 400,
    "entity": 300,
    "facts": 300,
    "web": 0,
}


@dataclass
class TierAllocation:
    """One tier's share of the budget, plus what it actually spent."""

    name: str
    budget: int
    used: int = 0
    items: int = 0
    dropped: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    @property
    def tokens(self) -> int:
        return self.budget // CHARS_PER_TOKEN


@dataclass
class BudgetPlan:
    """The result of an allocation: a budget per tier plus bookkeeping."""

    total: int
    tiers: dict[str, TierAllocation] = field(default_factory=dict)

    def budget_for(self, name: str) -> int:
        tier = self.tiers.get(name)
        return tier.budget if tier else 0

    def note_usage(self, name: str, used: int, items: int = 0, dropped: int = 0):
        tier = self.tiers.get(name)
        if tier is None:
            return
        tier.used = max(0, int(used))
        tier.items = max(0, int(items))
        tier.dropped = max(0, int(dropped))

    @property
    def used(self) -> int:
        return sum(t.used for t in self.tiers.values())

    def spare_after(self, *names: str) -> int:
        """Characters the named tiers were allotted and did not spend.

        Callers hand this to the next tier so a quiet turn's slack is reused
        rather than wasted. It is computed from the tiers processed so far
        rather than accumulated tier by tier, because an accumulator
        double-counts: a tier that overspends its own share by dipping into
        the spare still reports zero shortfall, so the same characters get
        offered again to the tier after it.
        """
        return max(
            0,
            sum(
                self.budget_for(name) - self.tiers[name].used
                for name in names
                if name in self.tiers
            ),
        )

    def summary(self) -> str:
        """One-line report for logs. Only tiers with a budget appear."""
        parts = [
            f"{name}={tier.used}/{tier.budget}"
            + (f"(-{tier.dropped})" if tier.dropped else "")
            for name, tier in self.tiers.items()
            if tier.budget
        ]
        return f"ctx {self.used}/{self.total} chars [" + " ".join(parts) + "]"

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "used": self.used,
            "tiers": {
                name: {
                    "budget": t.budget,
                    "used": t.used,
                    "items": t.items,
                    "dropped": t.dropped,
                }
                for name, t in self.tiers.items()
            },
        }


def _coerce_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def allocate(
    total: int,
    weights: dict[str, int] | None = None,
    minimums: dict[str, int] | None = None,
    disabled: set[str] | None = None,
    caps: dict[str, int] | None = None,
) -> BudgetPlan:
    """Split ``total`` characters across the tiers.

    ``weights``   relative share per tier; missing tiers fall back to the
                  defaults, a weight of 0 switches the tier off.
    ``minimums``  floor per tier — a tier below its floor is zeroed and its
                  share redistributed, because a starved tier is worse than
                  an absent one.
    ``disabled``  tiers the caller has turned off (control flags), zeroed
                  before any maths so their share goes to the others.
    ``caps``      per-tier ceiling; a tier never gets more than it can use,
                  and the excess is redistributed.

    Redistribution runs to a fixed point, so a cap freeing characters can
    lift another tier over its floor in the next pass.
    """
    total = max(0, _coerce_int(total, 0))
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        for key, val in weights.items():
            if key in w:
                w[key] = max(0, _coerce_int(val, w[key]))
    mins = dict(DEFAULT_MINIMUMS)
    if minimums:
        for key, val in minimums.items():
            if key in mins:
                mins[key] = max(0, _coerce_int(val, mins[key]))
    for name in disabled or ():
        w[name] = 0

    plan = BudgetPlan(total=total)
    if total <= 0:
        plan.tiers = {name: TierAllocation(name, 0) for name in TIER_ORDER}
        return plan

    ceilings = {
        name: max(0, _coerce_int((caps or {}).get(name), 0)) for name in TIER_ORDER
    }
    shares: dict[str, int] = dict.fromkeys(TIER_ORDER, 0)
    unsettled = [name for name in TIER_ORDER if w.get(name, 0) > 0]
    pool = total

    # One tier is settled per pass, so this terminates in at most
    # len(TIER_ORDER) passes. Settling in-loop (rather than settling every
    # violator at once) is what keeps the sum honest: each settled tier is
    # deducted from the pool before the next proportional split is computed,
    # so paid floors can never add up to more than the budget.
    while unsettled:
        weight_sum = sum(w[name] for name in unsettled)
        if weight_sum <= 0:
            break
        provisional = {name: pool * w[name] // weight_sum for name in unsettled}

        settled: tuple[str, int] | None = None
        # Caps first: a tier that cannot use its share must hand it back
        # before anyone measures themselves against a floor.
        for name in unsettled:
            cap = ceilings[name]
            if cap and provisional[name] > cap:
                settled = (name, cap)
                break
        if settled is None:
            for name in unsettled:
                floor = mins.get(name, 0)
                if provisional[name] >= floor:
                    continue
                # Pay the floor if the pool can afford it, otherwise drop the
                # tier: a fragment below the floor reads to the model as the
                # whole truth, which is worse than an absent section.
                pay = min(floor, ceilings[name]) if ceilings[name] else floor
                settled = (name, pay if pay <= pool else 0)
                break
        if settled is None:
            # Nobody is over a cap or under a floor — split what is left and
            # hand the integer-division remainder to the highest-priority
            # tier still in play rather than dropping it.
            for name in unsettled:
                shares[name] = provisional[name]
            shares[unsettled[0]] += max(
                0, pool - sum(provisional[name] for name in unsettled)
            )
            break

        name, amount = settled
        shares[name] = amount
        pool = max(0, pool - amount)
        unsettled.remove(name)

    plan.tiers = {
        name: TierAllocation(name, shares.get(name, 0)) for name in TIER_ORDER
    }
    return plan


def weights_from_control(control: dict) -> dict[str, int]:
    """Read per-tier weights out of the bot control dict."""
    out: dict[str, int] = {}
    for name in TIER_ORDER:
        key = f"context_tier_{name}_weight"
        if key in (control or {}):
            out[name] = max(0, min(100, _coerce_int(control.get(key), DEFAULT_WEIGHTS[name])))
    return out


def fit_lines(lines: list[str], budget: int, separator: str = "\n") -> tuple[list[str], int]:
    """Take lines in order until the next one would blow ``budget``.

    Returns ``(kept, dropped)``. Order is preserved and callers are expected
    to sort by relevance first — this drops from the tail, which is where
    the least relevant material already is.

    A budget too small even for the first line keeps nothing: a truncated
    first fact is worse than an absent one, since the model reads the
    fragment as complete.
    """
    if budget <= 0:
        return [], len(lines)
    kept: list[str] = []
    used = 0
    sep_len = len(separator)
    for i, line in enumerate(lines):
        cost = len(line) + (sep_len if kept else 0)
        if used + cost > budget:
            return kept, len(lines) - i
        kept.append(line)
        used += cost
    return kept, 0
