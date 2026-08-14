from bot import MaxwellBot
from bot_tools import _sanitize_web_query


GLUED = (
    "How much usage would I get with this on ollama cloud 20$ plan\n"
    "[Latest message replies to you/Maxwell(1382894657624866889): they don't "
    "publish a number. **ol"
)


def test_plain_user_text_strips_reply_glue():
    assert "Maxwell" not in MaxwellBot._plain_user_text(GLUED)
    assert "ollama cloud" in MaxwellBot._plain_user_text(GLUED).lower()


def test_extract_search_query_does_not_include_reply_blob():
    q = MaxwellBot._extract_search_query(GLUED)
    assert "Latest message replies" not in q
    assert "1382894657624866889" not in q
    assert "ollama" in q.lower()


def test_needs_up_to_date_ignores_glued_maxwell_reply():
    casual = (
        "lol\n[Latest message replies to you/Maxwell(1): glm 5.3 just released "
        "new model today]"
    )
    assert MaxwellBot._needs_up_to_date_info(casual) is False


def test_sanitize_web_query_truncates_unclosed_bracket():
    q = _sanitize_web_query(GLUED)
    assert "Latest message" not in q
    assert q.startswith("How much usage")
