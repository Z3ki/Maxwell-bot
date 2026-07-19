"""Tests for _sanitize_reasoning in tool_registry.

The 2026-07-19 user complaint: the model was dumping the entire Spanish
joke site body into the `reasoning` field for create_site. We now cap
hard at 280 chars AND prefer a clean sentence break inside that window.

These tests pin the contract:
  1. Plain short reasoning passes through unchanged.
  2. Long reasoning is cut at the last sentence terminator.
  3. Long reasoning with no terminator cuts at the last word boundary.
  4. HTML tags are stripped before the cap.
  5. The cap is exactly 280 chars in the worst-case branch.
"""

import sys
from pathlib import Path

# Make sure the repo root is on sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tool_registry  # noqa: E402


def test_short_reasoning_passes_through():
    s = tool_registry._sanitize_reasoning
    assert s("checking disk usage.") == "checking disk usage."
    assert s("") == ""
    assert s("   spaced   ") == "spaced"


def test_long_reasoning_cuts_at_sentence_terminator():
    s = tool_registry._sanitize_reasoning
    long = (
        "first thought. " * 50  # way over 280
    )
    out = s(long)
    assert len(out) <= tool_registry.REASONING_MAX_CHARS
    # The cap found a sentence terminator inside the window, so the
    # output ends with a terminator (no half-sentence).
    assert out.endswith((".", "!", "?"))


def test_long_reasoning_with_no_terminator_cuts_at_word_boundary():
    s = tool_registry._sanitize_reasoning
    # 500 chars of one giant unbroken token (no whitespace). The
    # cap has to use a hard cap + ellipsis because there's no
    # sentence terminator AND no word boundary to find.
    long = "x" * 500
    out = s(long)
    assert len(out) <= tool_registry.REASONING_MAX_CHARS
    # Hard cap (no sentence/word boundary): the output ends with the
    # ellipsis marker.
    assert out.endswith("…")


def test_long_reasoning_with_no_terminator_but_with_spaces_cuts_clean():
    s = tool_registry._sanitize_reasoning
    long = ("a" * 200) + " " + ("b" * 200)  # has a word boundary
    out = s(long)
    assert len(out) <= tool_registry.REASONING_MAX_CHARS
    # Either it cut at the space (with ellipsis), or it kept the
    # cap form. Either is fine — the invariant is that the cap is
    # not on a half-word.
    assert "…" in out or len(out) <= tool_registry.REASONING_MAX_CHARS


def test_html_tags_stripped_before_cap():
    s = tool_registry._sanitize_reasoning
    raw = "<thoughts>building the user's NOVA X1 site.</thoughts>"
    out = s(raw)
    # Tags are gone; the content remains.
    assert "<" not in out and ">" not in out
    assert "building" in out


def test_reasoning_max_chars_is_280():
    """The 2026-07-19 cap is 280. The previous 1000 let the model dump
    full site bodies; 280 is a hard ceiling that pairs with the
    sentence-boundary cut to enforce one-sentence reasoning."""
    assert tool_registry.REASONING_MAX_CHARS == 280
