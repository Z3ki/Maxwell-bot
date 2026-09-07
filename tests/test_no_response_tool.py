import asyncio
from types import SimpleNamespace

import pytest

from bot_tools import NoResponseTool


def _run(*, directed=False, control=None, row=None, reason="other"):
    message = SimpleNamespace(id=123)
    outcomes = []
    bot = SimpleNamespace(
        _control=control or {},
        _directly_addressed=lambda _message: directed,
        _request_journal=SimpleNamespace(get=lambda _id: row),
        _record_request_outcome=lambda *args, **kwargs: outcomes.append((args, kwargs)),
    )
    result = asyncio.run(NoResponseTool(bot).execute(message, reason=reason))
    return result, outcomes


def test_unanswered_direct_request_cannot_be_silenced_by_default():
    result, outcomes = _run(directed=True)
    assert result.startswith("Error:")
    assert "send_message" in result
    assert outcomes == []


def test_journaled_direct_request_cannot_be_silenced_if_address_resolution_changes():
    result, outcomes = _run(row={"directed": True, "status": "running"})
    assert result.startswith("Error:")
    assert outcomes == []


@pytest.mark.parametrize("enabled", [False, "false"])
def test_direct_silence_can_be_configured(enabled):
    result, outcomes = _run(
        directed=True,
        control={"require_direct_response": enabled},
        reason="user_requested_silence",
    )
    assert result == "__NO_RESPONSE__"
    assert outcomes[0][0][1] == "suppressed"
    assert outcomes[0][1]["reason"] == "model_no_response:user_requested_silence"


def test_unrelated_chatter_can_be_silenced_with_a_policy_reason():
    result, outcomes = _run(reason="unrelated")
    assert result == "__NO_RESPONSE__"
    assert outcomes[0][1]["reason"] == "model_no_response:unrelated"


def test_arbitrary_model_text_is_not_retained_as_a_reason():
    result, outcomes = _run(reason="private conversation text")
    assert result == "__NO_RESPONSE__"
    assert outcomes[0][1]["reason"] == "model_no_response:other"


def test_no_response_preserves_confirmed_delivery():
    result, outcomes = _run(
        directed=True, row={"status": "delivered", "directed": True}
    )
    assert result == "__NO_RESPONSE__"
    assert outcomes == []


def test_no_response_remains_compatible_without_lifecycle_hooks():
    result = asyncio.run(NoResponseTool(SimpleNamespace()).execute(SimpleNamespace()))
    assert result == "__NO_RESPONSE__"
