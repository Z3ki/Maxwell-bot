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
