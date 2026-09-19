"""Every registered tool has to exist in the live catalog.

A tool missing from one of them fails differently each time and none of the
failures point at the cause:

* no parameter schema — the model gets a nameless free-form object
* missing from ``KNOWN_TOOLS`` — the dashboard never lists it
* in neither contract set — it is treated as silent, and a turn that only
  called it goes out empty

Tools are discovered from plugin manifests. ``bot.py`` no longer contains a
hardcoded ``self.tools["name"] =`` registry.
"""

from control_defaults import KNOWN_TOOLS
from maxwell_core.plugins.catalog import discover_tool_names
from tool_schemas import (
    RESULT_TOOL_NAMES,
    TOOL_PARAMETERS,
    TURN_ENDING_TOOL_NAMES,
)

REGISTERED = discover_tool_names()


def test_the_scan_found_the_tool_registrations():
    assert len(REGISTERED) > 50


def test_every_registered_tool_has_a_parameter_schema():
    missing = [
        t
        for t in REGISTERED
        if t not in TOOL_PARAMETERS and t not in {
            # Plugin tools supply get_parameters() at runtime.
            "checkers_start",
            "checkers_move",
            "checkers_state",
            "checkers_resign",
            "reminder",
            "send_rich_message",
            "recall_cross_server_memory",
            "inspect_media_url",
            "plugin_workbench",
        }
    ]
    assert missing == []


def test_every_registered_tool_can_be_disabled_from_the_dashboard():
    assert [t for t in REGISTERED if t not in KNOWN_TOOLS] == []


def test_known_tools_has_no_ghosts():
    # Fallback names that plugins no longer ship are allowed to linger so
    # persisted disabled_tools lists still sanitize. The reverse — a live
    # plugin tool missing from KNOWN_TOOLS — is the failure.
    assert [t for t in REGISTERED if t not in KNOWN_TOOLS] == []


def test_no_tool_is_in_both_contract_sets():
    both = RESULT_TOOL_NAMES & TURN_ENDING_TOOL_NAMES
    assert not both, f"a tool cannot both end the turn and return a result: {both}"
