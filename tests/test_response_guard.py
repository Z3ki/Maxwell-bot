from response_guard import break_echo_loop, repetition_ratio, sanitize_transcript

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
