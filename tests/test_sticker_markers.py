"""Ordinary brackets stay text. Only explicit sticker markers send stickers."""

from types import SimpleNamespace

from discord_expressions import extract_stickers


def _bot(names):
    return SimpleNamespace(
        _standard_stickers={name.lower(): SimpleNamespace(name=name) for name in names}
    )


def test_bare_brackets_are_not_official_stickers():
    bot = _bot(["100", "Help", "OK", "Yes", "No"])
    samples = [
        "See the [Help](https://example.com/help) page for details.",
        "```python\nscores = data[100]\n```",
        "- [OK] build passed\n- [No] lint failed",
        "Answer: [Yes]",
    ]
    for text in samples:
        assert extract_stickers(bot, text, None) == (text, [])


def test_bare_server_sticker_name_stays_text():
    bot = _bot([])
    guild = SimpleNamespace(stickers=[SimpleNamespace(name="OK", available=True)])
    assert extract_stickers(bot, "[OK]", guild) == ("[OK]", [])


def test_explicit_sticker_markers_still_match():
    sticker = SimpleNamespace(name="Help")
    bot = SimpleNamespace(_standard_stickers={"help": sticker})
    for text in ("[STICKER (Help)]", "[STICKER: Help]", "[sticker Help]"):
        cleaned, found = extract_stickers(bot, f"note {text}", None)
        assert cleaned == "note"
        assert found == [sticker]
