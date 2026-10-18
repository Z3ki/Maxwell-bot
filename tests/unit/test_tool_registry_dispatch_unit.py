"""Tool registry, ToolSpec contracts, circuit breaker and argument prep."""

from __future__ import annotations

import pytest

from maxwell_core.tools import registry as registry_mod
from maxwell_core.tools.dispatch import (
    ToolCircuitBreaker,
    _prepare_tool_params,
    is_permission_denied_result,
)
from maxwell_core.tools.registry import ToolRegistry
from maxwell_core.tools.spec import (
    CONTRACT_ENDING,
    CONTRACT_RESULT,
    CONTRACT_SILENT,
    ToolSpec,
    platform_allowed,
    result_contract_label,
    spec_from_tool,
)


class _Tool:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    async def execute(self, message, **kwargs):
        return "ok"


def _spec(name="t", plugin="p", **kw):
    return ToolSpec(name=name, plugin=plugin, tool=kw.pop("tool", _Tool()), **kw)


# --------------------------------------------------------------------------
# dispatch helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, denied",
    [
        ("Error: Missing Permissions to ban", True),
        ("   error - permission denied here", True),
        ("Error: something else", False),
        ("ok: missing permissions", False),
        ("", False),
    ],
)
def test_permission_denied_detection(text, denied):
    assert is_permission_denied_result(text) is denied


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_breaker_opens_at_threshold_and_recovers():
    clock = _Clock()
    br = ToolCircuitBreaker(failure_threshold=3, recovery_seconds=30, clock=clock)
    br.record_failure("t")
    br.record_failure("t")
    assert not br.is_open("t")
    br.record_failure("t")
    assert br.is_open("t")
    clock.now += 29.9
    assert br.is_open("t")
    clock.now += 0.2
    assert not br.is_open("t")
    # Once recovered the open marker is cleared.
    assert "t" not in br._open_until


def test_breaker_only_counts_failures_in_the_last_minute():
    clock = _Clock()
    br = ToolCircuitBreaker(failure_threshold=2, recovery_seconds=5, clock=clock)
    br.record_failure("t")
    clock.now += 61
    br.record_failure("t")
    assert not br.is_open("t")
    assert len(br._failures["t"]) == 1


def test_breaker_success_resets_and_tools_are_isolated():
    clock = _Clock()
    br = ToolCircuitBreaker(failure_threshold=1, clock=clock)
    br.record_failure("a")
    assert br.is_open("a") and not br.is_open("b")
    br.record_success("a")
    assert not br.is_open("a")
    assert "a" not in br._failures


def test_prepare_params_drops_colliding_kwargs_and_does_not_mutate():
    params = {"message": "hi", "self": 1, "x": 2}
    out = _prepare_tool_params("other_tool", params)
    assert out == {"x": 2}
    assert params == {"message": "hi", "self": 1, "x": 2}
    assert _prepare_tool_params("any", None) == {}


@pytest.mark.parametrize(
    "params, content",
    [
        ({"message": "from message"}, "from message"),
        ({"text": "from text"}, "from text"),
        ({"body": "from body"}, "from body"),
        ({"message": "  ", "text": "t"}, "t"),
        ({"content": "kept", "message": "ignored"}, "kept"),
    ],
)
def test_prepare_params_send_message_content_aliases(params, content):
    assert _prepare_tool_params("send_message", params)["content"] == content


def test_prepare_params_send_message_without_any_body():
    assert "content" not in _prepare_tool_params("send_message", {"message": " "})


def test_prepare_params_create_thread_uses_message_as_opening():
    out = _prepare_tool_params("create_thread", {"message": "hello", "name": "n"})
    assert out == {"name": "n", "opening": "hello"}
    kept = _prepare_tool_params("create_thread", {"message": "x", "opening": "keep"})
    assert kept["opening"] == "keep"


# --------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transports, platform, allowed",
    [
        (("any",), "anything", True),
        ((), "discord", True),
        (None, "discord", True),
        ("discord", "discord", True),
        (("discord",), "user_install", True),
        (("user_install",), "discord", True),
        (("discord",), "web", False),
        (("web",), "discord", False),
    ],
)
def test_platform_allowed(transports, platform, allowed):
    assert platform_allowed(transports, platform) is allowed


def test_contract_and_labels():
    assert _spec(returns_result=True).contract == CONTRACT_RESULT
    assert _spec(ends_turn=True).contract == CONTRACT_ENDING
    assert _spec().contract == CONTRACT_SILENT
    assert result_contract_label("nope") == result_contract_label(CONTRACT_SILENT)


def test_schema_normalizes_and_copies():
    params = {"type": "object", "properties": {"a": {"type": "string"}}}
    spec = _spec(parameters=params)
    schema = spec.schema()
    assert schema["required"] == []
    schema["properties"]["a"]["type"] = "integer"
    assert params["properties"]["a"]["type"] == "string"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"type": "array"},
        {"properties": {"a": {}}},
        {"type": "object", "properties": []},
    ],
)
def test_schema_always_returns_an_object_schema(params):
    schema = _spec(parameters=params).schema()
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)


def test_openai_tool_truncates_description_and_labels_contract():
    spec = _spec(name="t", description="x" * 50, returns_result=True)
    fn = spec.openai_tool(max_description_chars=10)["function"]
    assert fn["description"] == "x" * 9 + "…" + " [returns output]"
    assert fn["name"] == "t"
    assert (
        _spec(description="").openai_tool()["function"]["description"].startswith("t")
    )
    bad = spec.openai_tool(max_description_chars="bad")["function"]["description"]  # type: ignore[arg-type]
    assert bad.startswith("x" * 50)


def test_spec_from_tool_reads_metadata_and_survives_broken_getters():
    def boom():
        raise RuntimeError("x")

    tool = _Tool(
        tool_name="fallback",
        get_description=boom,
        get_parameters=boom,
        transports="discord",
        required_capabilities=["net"],
        required_discord_permissions=("ban_members",),
        timeout_seconds="nope",
        returns_result=1,
        side_effects=0,
    )
    spec = spec_from_tool(tool, name="", plugin="p")
    assert spec.name == "fallback"
    assert spec.description == "fallback"
    assert spec.parameters == {}
    assert spec.transports == ("discord",)
    assert spec.required_capabilities == ("net",)
    assert spec.required_discord_permissions == ("ban_members",)
    assert spec.timeout_seconds is None
    assert spec.returns_result is True and spec.side_effects is False


def test_spec_from_tool_ignores_non_dict_parameters_and_parses_timeout():
    tool = _Tool(get_parameters=lambda: ["x"], timeout_seconds="2.5")
    spec = spec_from_tool(tool, name="n", plugin="p")
    assert spec.parameters == {} and spec.timeout_seconds == 2.5


def test_public_dict_has_no_tool_object():
    data = _spec(required_capabilities=("a",)).as_public_dict()
    assert "tool" not in data and data["required_capabilities"] == ["a"]


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def test_register_and_lookup():
    reg = ToolRegistry()
    tool = _Tool()
    reg.register(_spec(name=" t ", plugin=" p ", tool=tool))
    assert "t" in reg and len(reg) == 1 and list(reg) == ["t"]
    assert reg.tool("t") is tool and reg.tool("missing") is None
    assert reg.plugin_tools("p") == ["t"]
    # The live instance's name is synced to the registry key.
    assert tool.name == "t" and tool.tool_name == "t"


@pytest.mark.parametrize(
    "name, plugin", [("", "p"), ("  ", "p"), ("t", ""), ("t", " ")]
)
def test_register_requires_name_and_plugin(name, plugin):
    with pytest.raises(ValueError):
        ToolRegistry().register(_spec(name=name, plugin=plugin))


def test_shadowing_requires_override():
    reg = ToolRegistry()
    reg.register(_spec(plugin="a"))
    with pytest.raises(ValueError, match="cannot shadow"):
        reg.register(_spec(plugin="b"))
    reg.register(_spec(plugin="b", override=True))
    assert reg.get("t").plugin == "b"
    assert reg.plugin_tools("a") == [] and reg.plugin_tools("b") == ["t"]


def test_protected_tool_cannot_be_replaced_even_with_spec_override():
    reg = ToolRegistry()
    reg.register(_spec(plugin="core"), protected=True)
    with pytest.raises(ValueError, match="protected core capability"):
        reg.register(_spec(plugin="evil", override=True))
    reg.register(_spec(plugin="core2"), allow_override=True)
    assert reg.get("t").plugin == "core2"


def test_unregister_keeps_protected_tools_unless_forced():
    reg = ToolRegistry()
    reg.register(_spec(name="keep", plugin="p"), protected=True)
    reg.register(_spec(name="drop", plugin="p"))
    assert reg.unregister_plugin("p") == ["drop"]
    assert "keep" in reg and reg.plugin_tools("p") == ["keep"]
    assert reg.unregister_plugin("p", force=True) == ["keep"]
    assert len(reg) == 0 and reg.plugin_tools("p") == []


def test_unregister_does_not_remove_a_tool_taken_over_by_another_plugin():
    reg = ToolRegistry()
    reg.register(_spec(plugin="a"))
    reg.register(_spec(plugin="b"), allow_override=True)
    assert reg.unregister_plugin("a") == []
    assert reg.get("t").plugin == "b"


def test_visible_and_openai_tools_filters():
    reg = ToolRegistry()
    reg.register(_spec(name="pub", plugin="p"))
    reg.register(_spec(name="adm", plugin="p", requires_admin=True))
    reg.register(_spec(name="web", plugin="q", transports=("web",)))
    reg.register(_spec(name="off", plugin="q"))
    assert set(reg.visible()) == {"pub", "adm", "off"}
    assert set(reg.visible(include_admin=False)) == {"pub", "off"}
    assert set(reg.visible(disabled=["off"])) == {"pub", "adm"}
    assert set(reg.visible(allowed_plugins=["q"], platform="web")) == {"web", "off"}
    names = [
        t["function"]["name"] for t in reg.openai_tools(allowed_names=["pub", "web"])
    ]
    assert names == ["pub"]


def test_result_and_ending_names_and_contract_validation():
    reg = ToolRegistry()
    reg.register(_spec(name="r", plugin="p", returns_result=True))
    reg.register(_spec(name="e", plugin="p", ends_turn=True))
    reg.register(_spec(name="both", plugin="p", returns_result=True, ends_turn=True))
    reg.register(_spec(name="noexec", plugin="p", tool=object()))
    reg.register(_spec(name="badschema", plugin="p", parameters={"type": "array"}))
    assert reg.result_names() == {"r", "both"}
    assert reg.ending_names() == {"e"}
    problems = reg.validate_contracts()
    assert "both: cannot both return a result and end the turn" in problems
    assert "noexec: no executable implementation" in problems
    # schema() normalizes non-object parameters, so that is not a problem.
    assert not [p for p in problems if p.startswith("badschema")]


def test_snapshot_and_specs_are_sorted():
    reg = ToolRegistry()
    for name in ("b", "a", "c"):
        reg.register(_spec(name=name, plugin="p"))
    assert reg.names() == ["a", "b", "c"]
    assert [s.name for s in reg.specs()] == ["a", "b", "c"]
    assert set(reg.snapshot_as_dict()) == {"a", "b", "c"}


def test_register_tool_builds_spec_from_instance():
    reg = ToolRegistry()
    spec = reg.register_tool(
        _Tool(get_description=lambda: "desc"), name="made", plugin="p", protected=True
    )
    assert spec.description == "desc" and "made" in reg._protected


def test_global_registry_is_lazy_and_replaceable(monkeypatch):
    monkeypatch.setattr(registry_mod, "_GLOBAL", None)
    first = registry_mod.get_global_registry()
    assert registry_mod.get_global_registry() is first
    replacement = ToolRegistry()
    registry_mod.set_global_registry(replacement)
    assert registry_mod.get_global_registry() is replacement
