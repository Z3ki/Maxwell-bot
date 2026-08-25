"""Stopping the bot from repeating itself.

Two different problems that look like one. Inside a single reply,
"jajajajajaja" is a run that can be collapsed after generation —
`response_guard` has done that since it was written, but nothing called it, so
every run reached the channel intact. Across replies, the same phrase opening
six messages running is a pattern no single message is wrong for, so nothing
downstream can catch it; the model has to be told, because it reads its own
last reply as evidence of what it sounds like and does it again.
"""

from types import SimpleNamespace

from bot import MaxwellBot, _sanitize_visible_reply


# ─── within one reply ────────────────────────────────────────────────────


def test_a_laugh_run_is_collapsed_before_it_is_sent():
    assert _sanitize_visible_reply("jajajajajaja qué bueno") == "ja qué bueno"
    assert _sanitize_visible_reply("(ja)(ja)(ja)(ja) listo") == "(ja)(ja) listo"


def test_repeated_words_and_sentences_are_collapsed():
    assert _sanitize_visible_reply("y y de de acuerdo") == "y de acuerdo"
    assert _sanitize_visible_reply("Esto funciona. Esto funciona.") == "Esto funciona."


def test_ordinary_text_is_left_alone():
    text = "here is the deploy plan, it should take about ten minutes"
    assert _sanitize_visible_reply(text) == text


def test_code_blocks_are_never_scrubbed():
    # A program that legitimately repeats a line is not repetition.
    code = "```\nja ja ja ja\nprint(1)\nprint(1)\n```"
    assert _sanitize_visible_reply(code) == code


def test_an_echo_loop_is_truncated_rather_than_posted_whole():
    looped = "the same thing " * 40
    out = _sanitize_visible_reply(looped)
    assert len(out) < len(looped)


def test_the_scrub_can_be_switched_off():
    assert (
        _sanitize_visible_reply("jajajajajaja hola", scrub_repeats=False)
        == "jajajajajaja hola"
    )


def test_tool_trace_cleanup_still_happens():
    # The scrub is bolted onto an existing sanitizer; it must not displace it.
    assert "__NO_RESPONSE__" not in _sanitize_visible_reply("hi __NO_RESPONSE__")


# ─── across replies ──────────────────────────────────────────────────────


def _bot(control=None, self_id=1):
    return SimpleNamespace(
        _control=control if control is not None else {},
        user=SimpleNamespace(id=self_id),
    )


def _mine(*contents):
    return [{"author_is_bot": True, "content": c} for c in contents]


def test_a_repeated_opener_is_named_back_at_him():
    note = MaxwellBot._self_repetition_note(
        _bot(),
        _mine(
            "jajaja bro that is wild",
            "jajajaja man ok",
            "sure thing",
            "jaja yeah",
            "ok cool",
        ),
    )
    # Naming the phrase is the point — "vary your language" changes nothing.
    assert "jaja" in note
    assert "3 of your last 5" in note


def test_laugh_runs_of_different_lengths_count_as_one_tic():
    # "jajaja" and "jajajajaja" are the same habit; an exact-match check on the
    # opener would see two different strings and stay quiet.
    note = MaxwellBot._self_repetition_note(
        _bot(), _mine("jajaja a", "jajajajaja b", "jaja c")
    )
    assert note


def test_varied_openers_say_nothing():
    note = MaxwellBot._self_repetition_note(
        _bot(), _mine("hi", "there you go", "what is up", "ok then")
    )
    assert note == ""


def test_other_peoples_messages_are_not_his_habit():
    others = [
        {"author_is_bot": False, "author_id": "9", "content": "jajaja hey"}
        for _ in range(6)
    ]
    assert MaxwellBot._self_repetition_note(_bot(), others) == ""


def test_tool_rows_are_not_speech():
    rows = [
        {"author_is_bot": True, "is_tool": True, "content": "Called x with {}"}
        for _ in range(6)
    ]
    assert MaxwellBot._self_repetition_note(_bot(), rows) == ""


def test_an_empty_or_short_history_says_nothing():
    assert MaxwellBot._self_repetition_note(_bot(), []) == ""
    assert MaxwellBot._self_repetition_note(_bot(), None) == ""
    assert MaxwellBot._self_repetition_note(_bot(), _mine("jaja a", "jaja b")) == ""


def test_the_nudge_can_be_switched_off():
    note = MaxwellBot._self_repetition_note(
        _bot({"self_repetition_note_enabled": False}),
        _mine("jaja a", "jaja b", "jaja c"),
    )
    assert note == ""


def test_messages_attributed_by_id_count_too():
    # Not every stored row carries author_is_bot; some only have the id.
    rows = [
        {"author_id": "1", "content": c} for c in ("jaja a", "jaja b", "jaja c")
    ]
    assert MaxwellBot._self_repetition_note(_bot(self_id=1), rows)
