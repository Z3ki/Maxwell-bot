"""Web result snippet regression tests."""

from bot import _web_result_snippet


def test_web_result_snippet_strips_repeated_title():
    title = "Python asyncio guide"
    body = "Event loops, coroutines and tasks explained."
    # New-format row (title once).
    assert _web_result_snippet(f"{title}\n{body}", title) == body
    # Legacy row written before the fix (title twice).
    assert _web_result_snippet(f"{title}\n{title}\n{body}", title) == body
    # No title match — content passes through untouched.
    assert _web_result_snippet(body, "unrelated") == body
    # Missing title must not blow up.
    assert _web_result_snippet(f"{title}\n{body}", "") == f"{title}\n{body}"
    assert _web_result_snippet("", title) == ""


def test_web_result_snippet_respects_limit():
    assert len(_web_result_snippet("z" * 900, "t", limit=280)) == 280
