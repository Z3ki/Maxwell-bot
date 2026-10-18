from types import SimpleNamespace

import jsonschema
import pytest

from plugins.maxwell_extras.task_workers import DelegateTaskTool
from provider_limits import (
    WORKER_TIMEOUT_MAX_SECONDS,
    requested_worker_timeout_seconds,
    worker_timeout_seconds,
)


def _bot(**control):
    return SimpleNamespace(
        config=SimpleNamespace(AI_WORKER_TURN_TIMEOUT_SECONDS=0), _control=control
    )


def test_default_worker_budget_is_20_minutes():
    assert worker_timeout_seconds(_bot()) == 1200


def test_model_can_request_up_to_30_minutes():
    assert requested_worker_timeout_seconds(1200, None) == 1200
    assert requested_worker_timeout_seconds(1200, 25) == 1500
    assert requested_worker_timeout_seconds(1200, 30) == WORKER_TIMEOUT_MAX_SECONDS
    assert requested_worker_timeout_seconds(1200, 90) == WORKER_TIMEOUT_MAX_SECONDS
    assert requested_worker_timeout_seconds(1200, 5) == 300
    assert requested_worker_timeout_seconds(1200, "bad") == 1200
    assert requested_worker_timeout_seconds(1200, 0) == 1200


def test_schema_accepts_timeout_minutes_in_range():
    schema = DelegateTaskTool.parameters
    jsonschema.validate({"tasks": [{"task": "x"}], "timeout_minutes": 30}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"tasks": [{"task": "x"}], "timeout_minutes": 31}, schema)
