"""Tests for new-channel / ticket-channel detection and greetings."""

import channel_watch


def test_ticket_signals():
    assert channel_watch.is_ticket_channel("ticket")
    assert channel_watch.is_ticket_channel("tickets-1")
    assert channel_watch.is_ticket_channel("support")
    assert channel_watch.is_ticket_channel("help-desk")
    assert channel_watch.is_ticket_channel("helpdesk")
    assert channel_watch.is_ticket_channel("modmail")
    assert channel_watch.is_ticket_channel("mod-mail")
    assert channel_watch.is_ticket_channel("reports")
    assert channel_watch.is_ticket_channel("appeals")
    assert channel_watch.is_ticket_channel("bug-reports")
    assert channel_watch.is_ticket_channel("request")
    assert channel_watch.is_ticket_channel("open-a-ticket")


def test_ticket_negative_signals_win():
    assert not channel_watch.is_ticket_channel("general")
    assert not channel_watch.is_ticket_channel("chat")
    assert not channel_watch.is_ticket_channel("ticket-chat")
    assert not channel_watch.is_ticket_channel("support-general")
    assert not channel_watch.is_ticket_channel("memes")
    assert not channel_watch.is_ticket_channel("spam")
    assert not channel_watch.is_ticket_channel("lounge")


def test_normal_channels_are_not_tickets():
    assert not channel_watch.is_ticket_channel("general-chat")
    assert not channel_watch.is_ticket_channel("music")
    assert not channel_watch.is_ticket_channel("code")
    assert not channel_watch.is_ticket_channel("random")
    assert not channel_watch.is_ticket_channel("")


def test_channel_kind():
    assert channel_watch.channel_kind("ticket-42") == "ticket"
    assert channel_watch.channel_kind("general") == "normal"


def test_normalise_and_partial_matching():
    # A word that merely contains a signal must NOT match (no boundary).
    assert not channel_watch.is_ticket_channel("unhelpful")
    assert not channel_watch.is_ticket_channel("interticket-blocked")
    # Mixed separators normalise correctly.
    assert channel_watch.is_ticket_channel("Support-Tickets")
    assert channel_watch.channel_kind("need help here") == "ticket"


def test_greeting_is_short_and_on_point():
    g = channel_watch.greeting_for("ticket", "maxwell")
    assert "ticket" in g
    assert "maxwell" in g
    assert len(g) < 200


def test_default_ticket_greeting():
    assert channel_watch.default_ticket_greeting() is True
