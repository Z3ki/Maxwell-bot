from bot import MaxwellBot


def test_split_response_closes_and_reopens_code_fence_when_chunking():
    text = "```python\n" + ("print('x')\n" * 80) + "```"

    chunks = MaxwellBot._split_response(text, limit=180)

    assert len(chunks) > 1
    assert all(len(c) <= 188 for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_split_response_preserves_custom_filename_extensions_in_code_fence():
    text = "```lol.html\n" + ("<p>hey</p>\n" * 70) + "```"

    chunks = MaxwellBot._split_response(text, limit=170)

    assert len(chunks) > 1
    assert chunks[0].startswith("```lol.html")
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_split_response_does_not_wrap_trailing_prose_in_a_code_fence():
    """Text AFTER a chunked code block must render as prose.

    The fence-repair pass counted fences on the chunk *after* prepending its
    own re-opener, so the chunk carrying the block's closing fence looked
    balanced, the "inside a code block" flag was never cleared, and every
    following chunk of ordinary prose got wrapped in ``` and rendered as code.
    """
    code = "```python\n" + ("print('x')\n" * 40) + "```"
    prose = "That is the code. " + ("Hope it helps. " * 12)
    chunks = MaxwellBot._split_response(code + "\n\n" + prose, limit=180)

    assert len(chunks) > 2
    assert all(c.count("```") % 2 == 0 for c in chunks)
    # Every chunk that carries code is fenced...
    assert chunks[0].startswith("```python")
    # ...and the prose tail is not.
    assert "```" not in chunks[-1]
    assert "Hope it helps." in chunks[-1]


def test_split_response_reopens_fence_on_every_code_chunk():
    text = "```\n" + ("line of code\n" * 60) + "```"
    chunks = MaxwellBot._split_response(text, limit=200)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith("```")
        assert chunk.rstrip().endswith("```")
        assert chunk.count("```") == 2
