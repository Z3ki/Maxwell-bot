"""Malformed upstream metadata must not corrupt diagnostics or quota errors."""

import math

import pytest

from providers import (
    _coerce_token_count,
    _is_usage_exhausted_error,
    _normalize_llm_usage,
    _reported_cost_usd,
)


@pytest.mark.parametrize(
    "value", [True, False, None, -5, "bad", {}, [], float("nan"), float("inf")]
)
def test_invalid_token_counts_are_zero(value):
    assert _coerce_token_count(value) == 0


@pytest.mark.parametrize("total", [0, 1, 10, -1, "bad", None])
def test_usage_total_cannot_be_less_than_known_input_plus_output(total):
    usage = _normalize_llm_usage(
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": total}
    )
    assert usage["total_tokens"] == 15


def test_usage_retains_additional_billable_tokens_and_cost_aliases():
    usage = _normalize_llm_usage(
        {
            "prompt_eval_count": "10",
            "eval_count": "5.0",
            "total_tokens": 20,
            "cost_details": {"total": 0.001},
        }
    )
    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 20,
        "cost_usd": 0.001,
    }


@pytest.mark.parametrize("bad", [True, "secret", float("nan"), float("inf"), 10**400])
def test_invalid_cost_does_not_hide_valid_alternate_cost(bad):
    assert _reported_cost_usd({"cost": bad, "total_cost": 0.002}) == 0.002
    assert math.isfinite(_reported_cost_usd({"cost": bad}))


@pytest.mark.parametrize(
    "text",
    [
        "Provider quota exceeded",
        "Your project quota exceeded",
        "QuotaExhausted",
        "quota_exhausted",
    ],
)
def test_quota_exhaustion_without_a_specific_pooled_model_is_global(text):
    assert _is_usage_exhausted_error(429, text)


@pytest.mark.parametrize(
    "text",
    [
        "QuotaExhausted for gemini-3-flash",
        "rate limited: quota_exhausted for claude-sonnet",
        "too many requests",
    ],
)
def test_single_pooled_model_and_transient_rate_limits_remain_retryable(text):
    assert not _is_usage_exhausted_error(429, text)


def test_explicit_billing_exhaustion_remains_global_even_with_pooled_model_name():
    assert _is_usage_exhausted_error(429, "gemini-3-flash: insufficient credits")
    assert not _is_usage_exhausted_error(400, "Provider quota exceeded")
