"""Regression: the reaction path's fake_reply must accept `content=` as a keyword.

_send_with_slowmode dispatches every reply as `reply_to.reply(content=..., ...)`.
On the reaction path `reply_to` is a SimpleNamespace whose `.reply` is a local
closure, so that closure's first parameter name is part of the contract with
the real send path — not a free choice.

It was named `reply_content`, so `content=` fell through into **kwargs while
the positional stayed None. The closure then forwarded None positionally AND
content by keyword:

    PartialMessage.reply() got multiple values for argument 'content'

which killed every reaction-triggered reply (reaction_replies defaults on).

bot.py is far too heavy to import for a unit test and the closure is local to
a handler, so this checks the contract statically with ast. That also keeps it
robust against reformatting, which a source-regex approach is not.
"""

import ast
import asyncio
from pathlib import Path

import pytest

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"


def _find_fake_reply() -> ast.AsyncFunctionDef:
    tree = ast.parse(BOT_PY.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "fake_reply":
            return node
    raise AssertionError("fake_reply not found in bot.py — did it move or get renamed?")


def test_fake_reply_first_parameter_is_named_content():
    """The parameter name IS the bug surface: _send_with_slowmode passes it by keyword."""
    fn = _find_fake_reply()
    args = [a.arg for a in fn.args.args]
    assert args and args[0] == "content", (
        f"fake_reply's first parameter is {args[0]!r}, must be 'content' — "
        "_send_with_slowmode calls reply_to.reply(content=...) as a keyword, so any "
        "other name lets content fall into **kwargs and collide with the positional."
    )


def test_fake_reply_forwards_content_without_double_binding():
    """Every inner .reply()/.send() call must pass content once, positionally."""
    fn = _find_fake_reply()
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    forwards = [
        c
        for c in calls
        if isinstance(c.func, ast.Attribute) and c.func.attr in {"reply", "send"}
    ]
    assert forwards, "expected fake_reply to forward to .reply()/.send()"
    for call in forwards:
        kwarg_names = {k.arg for k in call.keywords}
        assert "content" not in kwarg_names, (
            "content is forwarded BOTH positionally and by keyword — that is "
            "exactly the TypeError this test exists to prevent."
        )
        assert call.args, "content must still be forwarded positionally"
        assert isinstance(call.args[0], ast.Name) and call.args[0].id == "content"


def test_the_original_crash_is_reproducible():
    """Guard the guard: prove this failure mode is real and would have been caught.

    Mirrors discord.py's signature, where `content` is the first
    positional-or-keyword parameter — which is what makes the double-bind possible.
    """
    calls = []

    async def discordish_reply(content=None, **kwargs):
        calls.append((content, kwargs))
        return "sent"

    async def old_fake_reply(reply_content=None, **kwargs):
        return await discordish_reply(reply_content, **kwargs)

    async def fixed_fake_reply(content=None, **kwargs):
        return await discordish_reply(content, **kwargs)

    with pytest.raises(TypeError, match="multiple values for argument 'content'"):
        asyncio.run(old_fake_reply(content="hello"))

    assert asyncio.run(fixed_fake_reply(content="hello")) == "sent"
    assert calls == [("hello", {})]
