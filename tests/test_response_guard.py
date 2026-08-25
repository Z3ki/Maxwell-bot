from response_guard import break_echo_loop, repetition_ratio, sanitize_transcript, scrub_repetitions


def test_transcript_deduplicates_adjacent_echoes():
    result = sanitize_transcript([{"role":"assistant","content":" hi  there "},{"role":"assistant","content":"hi there"}])
    assert len(result) == 1


def test_repetition_guard_breaks_echo():
    text = "one two three one two three one two three"
    assert repetition_ratio(text) > .5
    assert break_echo_loop(text).endswith("…")


def test_transcript_trims_after_deduplication():
    result = sanitize_transcript([{"role":"user","content":"a"*10},{"role":"assistant","content":"b"*10}], max_chars=10)
    assert len(result) == 1


def test_scrubs_spanish_and_mixed_laugh_loops():
    assert scrub_repetitions("jajajajajaja qué bueno") == "ja qué bueno"
    assert scrub_repetitions("(ja)(ja)(ja)(ja) listo") == "(ja)(ja) listo"


def test_scrubs_duplicate_words_and_repeated_phrases():
    assert scrub_repetitions("y y de de acuerdo") == "y de acuerdo"
    assert scrub_repetitions("Esto funciona. Esto funciona.") == "Esto funciona."


def test_scrubs_repetitive_loops_and_punctuation_but_preserves_code():
    text = "uno dos tres, uno dos tres, uno dos tres!!!\n```python\nprint('ja ja ja!!!')\n```"
    cleaned = scrub_repetitions(text)
    assert cleaned.startswith("uno dos tres")
    assert cleaned.count("uno dos tres") == 1
    assert "print('ja ja ja!!!')" in cleaned


def test_scrubbing_a_long_reply_is_not_quadratic():
    """A wall of prose with no full stop used to take minutes.

    The sentence rule was one regex with a backreference over an unbounded
    word run, and the chunk scan rescanned from every position when no
    terminator followed — 3.8s on one 19KB message, then worse. This runs on
    every outgoing message, synchronously, in the event loop, so "slow" here
    means the whole bot stops answering everyone.
    """
    import time

    text = " ".join(f"w{i}" for i in range(4000))  # ~24KB, not one full stop
    start = time.perf_counter()
    assert scrub_repetitions(text) == text
    assert time.perf_counter() - start < 1.0


def test_repeated_sentences_still_collapse_case_insensitively():
    assert scrub_repetitions("Esto funciona. esto FUNCIONA.") == "Esto funciona."
    assert scrub_repetitions("Se fue. Se fue. Se fue.") == "Se fue."


def test_identical_list_items_are_a_list_not_a_stutter():
    text = "- Item.\n- Item."
    assert scrub_repetitions(text) == text
