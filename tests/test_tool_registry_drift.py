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
    assert [t for t in REGISTERED if t not in KNOWN_TOOLS] == []


def test_known_tools_has_no_ghosts():
    # Fallback names that plugins no longer ship are allowed to linger so
    # persisted disabled_tools lists still sanitize. The reverse — a live
    # plugin tool missing from KNOWN_TOOLS — is the failure.
    assert [t for t in REGISTERED if t not in KNOWN_TOOLS] == []


def test_no_tool_is_in_both_contract_sets():
    both = RESULT_TOOL_NAMES & TURN_ENDING_TOOL_NAMES
    assert not both, f"a tool cannot both end the turn and return a result: {both}"
