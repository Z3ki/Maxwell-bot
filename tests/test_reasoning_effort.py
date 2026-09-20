"""Provider reasoning_effort passthrough."""

import providers
from providers import OllamaProvider, ProviderEndpoint


def _primary(reasoning_effort=""):
    p = OllamaProvider(
        "http://127.0.0.1:8317/v1",
        "grok-4.6",
        100,
        0.7,
        api_key="k",
        disable_reasoning=False,
        reasoning_effort=reasoning_effort,
    )
    return p, p._endpoints[0]


def test_reasoning_effort_in_primary_payload():
    p, ep = _primary("low")
    assert ep.name == "primary"
    data = p._request_payload(ep, [{"role": "user", "content": "hi"}])
    assert data["reasoning_effort"] == "low"
    assert data["model"] == "grok-4.6"


def test_no_effort_by_default():
    p, ep = _primary()
    data = p._request_payload(ep, [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in data


def test_effort_not_sent_on_fallback_endpoint():
    p = OllamaProvider(
        "http://127.0.0.1:8317/v1",
        "grok-4.6",
        100,
        0.7,
        api_key="k",
        fallback_base_url="http://127.0.0.1:8317/v1",
        fallback_model="other-model",
        disable_reasoning=False,
        fallback_disable_reasoning=False,
        reasoning_effort="low",
    )
    fb = next(e for e in p._endpoints if e.name == "fallback")
    data = p._request_payload(fb, [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in data
    assert data["model"] == "other-model"


def test_fallback_reasoning_effort_in_payload():
    p = OllamaProvider(
        "http://127.0.0.1:8317/v1",
        "grok-4.6",
        100,
        0.7,
        api_key="k",
        fallback_base_url="https://openrouter.ai/api/v1",
        fallback_model="meta/muse-spark-1.3-contributor",
        fallback_api_key="or",
        disable_reasoning=False,
        fallback_disable_reasoning=False,
        fallback_reasoning_effort="minimal",
        reasoning_effort="low",
    )
    fb = next(e for e in p._endpoints if e.name == "fallback")
    data = p._request_payload(fb, [{"role": "user", "content": "hi"}])
    assert data["model"] == "meta/muse-spark-1.3-contributor"
    assert data["reasoning_effort"] == "minimal"
    primary = next(e for e in p._endpoints if e.name == "primary")
    pdata = p._request_payload(primary, [{"role": "user", "content": "hi"}])
    assert pdata["reasoning_effort"] == "low"


def test_disable_reasoning_still_wins():
    p, ep = _primary("low")
    data = p._request_payload(
        ep, [{"role": "user", "content": "hi"}], disable_reasoning=True
    )
    assert data["reasoning_effort"] == "none"


def test_endpoint_field_defaults_empty():
    assert ProviderEndpoint("primary", "http://x/v1", "m").reasoning_effort == ""
    assert providers.OllamaProvider(
        "http://x/v1", "m", 10, 0.5
    ).reasoning_effort == ""
