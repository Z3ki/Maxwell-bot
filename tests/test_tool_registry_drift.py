"""Every registered tool has to exist in all four places that describe it.

A tool missing from one of them fails differently each time and none of the
failures point at the cause:

* no entry in ``TOOL_PARAMETERS`` — the model gets a nameless free-form object
* missing from ``KNOWN_TOOLS`` — the dashboard never lists it, and
  ``api/state.py`` silently strips it out of ``disabled_tools``, so the tool
  cannot be turned off through the API at all
* in neither contract set — it is treated as silent, and a turn that only
  called it goes out empty

This is a static scan of bot.py's registrations because constructing the bot
needs a Discord connection.
"""

import re
from pathlib import Path

from control_defaults import KNOWN_TOOLS
from tool_schemas import (
    RESULT_TOOL_NAMES,
    TOOL_PARAMETERS,
    TURN_ENDING_TOOL_NAMES,
)

REGISTERED = sorted(
    set(
        re.findall(
            r'self\.tools\["([a-z_0-9]+)"\]\s*=',
            Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(),
        )
    )
)


def test_the_scan_found_the_tool_registrations():
    # If this ever drops to a handful, the registration style changed and the
    # rest of this file is quietly testing nothing.
    assert len(REGISTERED) > 50


def test_every_registered_tool_has_a_parameter_schema():
    assert [t for t in REGISTERED if t not in TOOL_PARAMETERS] == []


def test_every_registered_tool_can_be_disabled_from_the_dashboard():
    assert [t for t in REGISTERED if t not in KNOWN_TOOLS] == []


def test_known_tools_has_no_ghosts():
    assert [t for t in KNOWN_TOOLS if t not in REGISTERED] == []


def test_no_tool_is_in_both_contract_sets():
    both = RESULT_TOOL_NAMES & TURN_ENDING_TOOL_NAMES
    assert not both, f"a tool cannot both end the turn and return a result: {both}"
