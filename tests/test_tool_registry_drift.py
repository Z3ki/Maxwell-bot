"""Live tools remain configurable and result/turn-ending contracts stay disjoint.

Plugin parameter schemas come from get_parameters() at runtime; a second
hardcoded schema-exception list is not a valid consumer contract.
"""

from control_defaults import KNOWN_TOOLS
from maxwell_core.plugins.catalog import discover_tool_names
from tool_schemas import (
    RESULT_TOOL_NAMES,
    TURN_ENDING_TOOL_NAMES,
)

REGISTERED = discover_tool_names()




def test_every_registered_tool_can_be_disabled_from_the_dashboard():
    # Retired names (in KNOWN_TOOLS, no longer shipped) are allowed to linger so
    # a persisted disabled_tools list still sanitizes; the failure direction is
    # a live plugin tool missing from KNOWN_TOOLS.
    assert [t for t in REGISTERED if t not in KNOWN_TOOLS] == []


def test_no_tool_is_in_both_contract_sets():
    both = RESULT_TOOL_NAMES & TURN_ENDING_TOOL_NAMES
    assert not both, f"a tool cannot both end the turn and return a result: {both}"
