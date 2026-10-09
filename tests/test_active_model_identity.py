"""Regression coverage for model identity across provider failover."""

import asyncio

from test_provider_architecture import _router
from test_providers import FakeErrorResponse, FakeResponse


def test_model_identity_follows_fallback_without_mutating_cached_prefix():
    router = _router(
        [FakeErrorResponse(503, "down")],
        [FakeResponse()],
        retry_attempts=2,
    )
    messages = [
        {"role": "system", "content": "Stable instructions"},
        {"role": "user", "content": "Who are you running on?"},
    ]
    original = [dict(m) for m in messages]
    result = asyncio.run(
        router.generate_response(messages, model="custom-primary", fast_fallback=True)
    )
    assert result.provider == "fallback"
    assert messages == original
    primary = router.primary._session.payloads[0]["messages"]
    fallback = router.fallback._session.payloads[0]["messages"]
    assert primary[0] == fallback[0] == messages[0]
    assert primary[-1] == fallback[-1] == messages[-1]
    assert "custom-primary" in primary[-2]["content"]
    assert "primary" in primary[-2]["content"]
    assert "fallback-model" in fallback[-2]["content"]
    assert "Route: fallback" in fallback[-2]["content"]
    assert "custom-primary" not in fallback[-2]["content"]
    assert fallback[-2]["role"] == "system"


def test_model_identity_on_direct_primary_response():
    router = _router([FakeResponse()], [], retry_attempts=1)
    asyncio.run(router.generate_response([{"role": "user", "content": "hello"}]))
    prompt = router.primary._session.payloads[0]["messages"]
    assert "primary-model" in prompt[-2]["content"]
    assert prompt[-1] == {"role": "user", "content": "hello"}
