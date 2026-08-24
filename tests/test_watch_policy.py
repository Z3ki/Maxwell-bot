"""The watch adapts to the room, and extraction stops keying off phrasing."""

import math

from watch_policy import (
    EXTRACT_THRESHOLD,
    AddressSignal,
    ExtractionContext,
    WatchState,
    debounce_seconds,
    describe_signal,
    extraction_score,
    name_mentioned,
    reply_pressure,
    should_extract,
    window_seconds,
)

BASE = 180.0


# --------------------------------------------------------------------------
# Watch window
# --------------------------------------------------------------------------


def test_a_room_ignoring_him_falls_out_of_watch_sooner():
    engaged = WatchState(engaged_at=1000.0)
    ignored = WatchState(engaged_at=1000.0, silent_streak=4)
    assert window_seconds(engaged, BASE, 1000.0) > BASE
    assert window_seconds(ignored, BASE, 1000.0) < BASE


def test_silence_shortens_the_window_monotonically():
    windows = [
        window_seconds(WatchState(engaged_at=1000.0, silent_streak=n), BASE, 1000.0)
        for n in range(5)
    ]
    assert windows == sorted(windows, reverse=True)
    assert windows[-1] < windows[0] / 2


def test_engagement_decays_over_the_configured_window():
    fresh = WatchState(engaged_at=1000.0)
    stale = WatchState(engaged_at=1000.0)
    assert window_seconds(fresh, BASE, 1000.0) > window_seconds(stale, BASE, 1600.0)


def test_a_cold_room_still_gets_a_usable_window():
    assert 0 < window_seconds(WatchState(), BASE, 5000.0) <= BASE * 2


def test_disabling_the_watch_is_respected():
    assert window_seconds(WatchState(engaged_at=1.0), 0.0, 2.0) == 0.0


def test_speaking_resets_the_patience_counter():
    state = WatchState(silent_streak=3)
    state.observe_spoke(100.0)
    assert state.silent_streak == 0
    assert state.spoke_at == 100.0


def test_being_addressed_resets_it_too():
    state = WatchState(silent_streak=3)
    state.observe_engagement(50.0)
    assert state.silent_streak == 0


# --------------------------------------------------------------------------
# Debounce
# --------------------------------------------------------------------------


def test_a_fast_room_waits_longer_so_a_burst_is_one_turn():
    fast = WatchState()
    for tick in range(6):
        fast.observe_message(tick * 0.2)
    assert debounce_seconds(fast, 1.0) < 1.0  # sub-second pace, quick reply

    busy = WatchState()
    for tick in range(6):
        busy.observe_message(tick * 2.0)
    assert debounce_seconds(busy, 1.0) > 1.0  # 2s pace, wait out the burst


def test_an_unmeasured_room_uses_the_configured_default():
    assert debounce_seconds(WatchState(), 1.0) == 1.0


def test_the_wait_stays_inside_its_envelope():
    glacial = WatchState()
    for tick in range(6):
        glacial.observe_message(tick * 90.0)
    assert debounce_seconds(glacial, 1.0) <= 4.0


def test_a_long_silence_is_not_mistaken_for_the_rooms_rhythm():
    state = WatchState()
    state.observe_message(0.0)
    state.observe_message(3600.0)
    assert state.interval_ema <= 120.0


# --------------------------------------------------------------------------
# Reply pressure
# --------------------------------------------------------------------------


def test_a_hard_ping_is_always_maximum_pressure():
    ignored = WatchState(silent_streak=9)
    assert reply_pressure(AddressSignal(direct=True), ignored, 0.0, BASE) == 1.0


def test_a_bot_line_asks_nothing_of_him():
    assert reply_pressure(AddressSignal(from_bot=True), WatchState(), 0.0, BASE) == 0.0


def test_being_named_beats_ambient_chatter():
    state = WatchState(engaged_at=1000.0)
    named = AddressSignal(names_him=True, text_length=20)
    ambient = AddressSignal(text_length=20)
    assert reply_pressure(named, state, 1000.0, BASE) > reply_pressure(
        ambient, state, 1000.0, BASE
    )


def test_a_line_aimed_at_someone_else_reads_as_lower_pressure():
    state = WatchState(engaged_at=1000.0)
    at_them = AddressSignal(reply_to_other=True, text_length=20)
    at_nobody = AddressSignal(text_length=20)
    assert reply_pressure(at_them, state, 1000.0, BASE) < reply_pressure(
        at_nobody, state, 1000.0, BASE
    )


def test_pressure_drops_in_a_room_that_keeps_ignoring_him():
    fresh = WatchState(engaged_at=1000.0)
    tired = WatchState(engaged_at=1000.0, silent_streak=4)
    signal = AddressSignal(names_him=True, text_length=20)
    assert reply_pressure(signal, tired, 1000.0, BASE) < reply_pressure(
        signal, fresh, 1000.0, BASE
    )


def test_pressure_is_a_probability():
    for state in (WatchState(), WatchState(engaged_at=1.0, silent_streak=9)):
        for signal in (
            AddressSignal(),
            AddressSignal(names_him=True, soft=True, is_question=True),
            AddressSignal(reply_to_other=True, mentions_other=True, text_length=1),
        ):
            assert 0.0 <= reply_pressure(signal, state, 1.0, BASE) <= 1.0


def test_his_name_follows_a_rename_rather_than_a_word_list():
    assert name_mentioned("hey maxwell you there", ["Maxwell"]) is True
    assert name_mentioned("hey MAXWELL", ["maxwell"]) is True
    # Substring hits don't count: he is not being addressed here.
    assert name_mentioned("maxwellian physics", ["maxwell"]) is False
    # A one- or two-letter nickname is too noisy to match on.
    assert name_mentioned("ok", ["ok"]) is False


def test_the_prompt_states_the_situation_instead_of_repeating_the_rule():
    text = describe_signal(
        AddressSignal(reply_to_other=True),
        WatchState(spoke_at=900.0, silent_streak=2, interval_ema=3.0),
        0.31,
        1000.0,
    )
    assert "reply to someone else" in text
    assert "100s ago" in text
    assert "last 2 time(s)" in text
    assert "0.31" in text


# --------------------------------------------------------------------------
# Context extraction
# --------------------------------------------------------------------------


def test_reactions_are_not_worth_a_call():
    for chatter in ("lol", "ok", "hahahaha", "EZE", "k", "?"):
        assert should_extract(ExtractionContext(text=chatter)) is False


def test_a_specific_statement_is():
    ctx = ExtractionContext(
        text="The staging box is prod-2 and Ana owns the DNS at https://z3ki.dev"
    )
    assert should_extract(ctx) is True


def test_the_same_fact_phrased_differently_still_lands():
    """The old trigger list caught the first of these and dropped the rest."""
    for phrasing in (
        "my name is Zeke, call me Z",
        "everyone just calls me Z, that's the name I go by",
        "llamame Z, es como me dice todo el mundo",
    ):
        assert should_extract(ExtractionContext(text=phrasing)) is True


def test_repetition_scores_below_variety():
    padded = extraction_score(ExtractionContext(text="haha " * 12)).value
    varied = extraction_score(
        ExtractionContext(text="the deploy box reboots nightly at four in the morning")
    ).value
    assert padded < varied


def test_a_room_that_just_stored_something_has_to_clear_a_higher_bar():
    marginal = ExtractionContext(text="I prefer dark mode")
    assert should_extract(marginal) is True
    marginal.since_last_extract = 30
    assert should_extract(marginal) is False


def test_but_something_specific_still_gets_through_immediately():
    ctx = ExtractionContext(
        text="The staging box is prod-2 and Ana owns the DNS at https://z3ki.dev",
        since_last_extract=0,
    )
    assert should_extract(ctx) is True


def test_an_admin_dm_is_the_highest_signal_channel_he_has():
    plain = ExtractionContext(text="switching us to Postgres next week")
    dm = ExtractionContext(
        text="switching us to Postgres next week", is_dm=True, author_is_admin=True
    )
    assert extraction_score(dm).value > extraction_score(plain).value


def test_media_alone_can_be_worth_a_look():
    assert extraction_score(ExtractionContext(text="", has_attachments=True)).value > 0


def test_an_empty_message_scores_nothing():
    assert extraction_score(ExtractionContext(text="")).value == 0.0


def test_the_score_is_bounded_and_explains_itself():
    ctx = ExtractionContext(
        text="the box Ana runs is prod-2, see https://z3ki.dev for the schedule",
        is_dm=True,
        author_is_admin=True,
        author_seen_before=False,
        has_attachments=True,
    )
    score = extraction_score(ctx)
    assert 0.0 <= score.value <= 1.0
    assert any("proper-noun" in reason for reason in score.reasons)
    assert any("link" in reason for reason in score.reasons)


def test_the_default_threshold_is_deliberately_permissive():
    assert 0.0 < EXTRACT_THRESHOLD < 0.5
    assert not math.isnan(EXTRACT_THRESHOLD)
