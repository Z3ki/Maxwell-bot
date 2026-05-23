from bot import render_custom_emoji_aliases


def test_render_custom_emoji_aliases_replaces_known_names_case_insensitive():
    rendered = render_custom_emoji_aliases(
        "hi :catjam: :DAVE: :missing:",
        {"catjam": "<a:catjam:123>", "dave": "<:dave:456>"},
    )

    assert rendered == "hi <a:catjam:123> <:dave:456> :missing:"


def test_render_custom_emoji_aliases_preserves_existing_discord_markup():
    rendered = render_custom_emoji_aliases(
        "already <:dave:456> and <a:catjam:123>",
        {"catjam": "<a:catjam:123>", "dave": "<:dave:456>"},
    )

    assert rendered == "already <:dave:456> and <a:catjam:123>"


def test_render_custom_emoji_aliases_fixes_broken_discord_markup():
    rendered = render_custom_emoji_aliases(
        "broken <:dave:> and <a:catjam:>",
        {"catjam": "<a:catjam:123>", "dave": "<:dave:456>"},
    )

    assert rendered == "broken <:dave:456> and <a:catjam:123>"
