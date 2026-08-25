"""Per-tier context budget allocation.

The one invariant that matters: an allocation never hands out more than it
was given. Everything above it in bot.py trusts that, because the transcript
is sized from what is left over.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_budget import (  # noqa: E402
    DEFAULT_MINIMUMS,
    DEFAULT_WEIGHTS,
    TIER_ORDER,
    allocate,
    fit_lines,
    weights_from_control,
)


def _total(plan):
    return sum(t.budget for t in plan.tiers.values())


def test_never_hands_out_more_than_the_total():
    # Includes the degenerate small budgets, which is where an earlier version
    # paid three tiers their floor out of a pool that could afford one.
    for total in (0, 1, 100, 500, 999, 1000, 5000, 50_000, 180_000, 2_000_000):
        plan = allocate(total)
        assert _total(plan) <= total, f"overshoot at total={total}"


def test_default_split_matches_the_weights():
    plan = allocate(100_000)
    for name, weight in DEFAULT_WEIGHTS.items():
        assert plan.budget_for(name) == weight * 1000


def test_disabled_tiers_give_their_share_to_the_others():
    plan = allocate(100_000, disabled={"web", "entity"})
    assert plan.budget_for("web") == 0
    assert plan.budget_for("entity") == 0
    # The whole budget is still spent — a disabled tier must not leave a hole.
    assert _total(plan) == 100_000
    assert plan.budget_for("recent") > DEFAULT_WEIGHTS["recent"] * 1000


def test_a_capped_tier_returns_the_excess():
    plan = allocate(100_000, caps={"recent": 5_000})
    assert plan.budget_for("recent") == 5_000
    assert _total(plan) == 100_000
    assert plan.budget_for("ltm") > DEFAULT_WEIGHTS["ltm"] * 1000


def test_every_tier_capped_spends_only_what_is_wanted():
    caps = {"recent": 5000, "ltm": 1000, "entity": 500, "facts": 500, "web": 100}
    plan = allocate(100_000, caps=caps)
    assert {n: plan.budget_for(n) for n in TIER_ORDER} == caps


def test_a_tier_under_its_floor_is_dropped_not_starved():
    # 1000 chars cannot pay the transcript's floor, so it is dropped rather
    # than given a two-line transcript that reads as the whole conversation.
    plan = allocate(1000)
    assert plan.budget_for("recent") == 0
    assert _total(plan) <= 1000


def test_zero_weight_switches_a_tier_off():
    plan = allocate(100_000, weights={"recent": 0})
    assert plan.budget_for("recent") == 0
    assert _total(plan) == 100_000


def test_weights_come_from_the_control_set():
    control = {f"context_tier_{n}_weight": 20 for n in TIER_ORDER}
    assert weights_from_control(control) == dict.fromkeys(TIER_ORDER, 20)
    # Out-of-range values are clamped rather than trusted.
    assert weights_from_control({"context_tier_ltm_weight": -5})["ltm"] == 0
    assert weights_from_control({"context_tier_ltm_weight": 9999})["ltm"] == 100
    # Keys nobody set are simply absent, so allocate() uses its defaults.
    assert weights_from_control({}) == {}


def test_garbage_weights_fall_back_to_the_default():
    plan = allocate(100_000, weights={"recent": "not a number"})
    assert plan.budget_for("recent") == DEFAULT_WEIGHTS["recent"] * 1000


def test_zero_budget_yields_every_tier_at_zero():
    plan = allocate(0)
    assert set(plan.tiers) == set(TIER_ORDER)
    assert _total(plan) == 0


def test_fit_lines_drops_from_the_tail():
    kept, dropped = fit_lines(["aaa", "bbb", "ccc"], 8)
    assert kept == ["aaa", "bbb"]
    assert dropped == 1


def test_fit_lines_keeps_nothing_rather_than_half_a_line():
    # A truncated first fact reads to the model as the complete fact.
    assert fit_lines(["a long fact"], 4) == ([], 1)
    assert fit_lines(["anything"], 0) == ([], 1)


def test_usage_reporting_and_remaining():
    plan = allocate(100_000)
    plan.note_usage("ltm", 3000, items=4, dropped=2)
    assert plan.tiers["ltm"].used == 3000
    assert plan.tiers["ltm"].remaining == plan.budget_for("ltm") - 3000
    assert plan.used == 3000
    summary = plan.summary()
    assert "ltm=3000/12000(-2)" in summary
    assert plan.as_dict()["tiers"]["ltm"]["items"] == 4


def test_minimums_are_honored_when_affordable():
    plan = allocate(20_000, weights={"recent": 99, "ltm": 1})
    # ltm's 1% of 20k is 200, under its 400 floor, so it is paid the floor.
    assert plan.budget_for("ltm") == DEFAULT_MINIMUMS["ltm"]
    assert _total(plan) <= 20_000
