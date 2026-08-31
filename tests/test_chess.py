"""Tests for the chess engine + manager (no Discord needed).

Covers the pure logic in ``chess_game.py``: board rendering, the move search,
the opening book, SAN/UCI parsing, and the one-game-per-channel, one-player
focus rule. The Discord-facing ``chess_*`` tools are thin wrappers on top and
are exercised in the live bot.
"""

from __future__ import annotations


from types import SimpleNamespace

import chess
import pytest

import chess_game as cg


@pytest.fixture
def manager(tmp_path):
    return cg.ChessManager(store_path=str(tmp_path / "chess_games.json"))


def test_initial_position():
    b = chess.Board()
    assert b.fen().startswith("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR")
    assert len(list(b.legal_moves)) == 20


def test_render_png_is_png():
    b = chess.Board()
    png = cg.render_board_png(b)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 5000  # a real board is larger than an empty canvas


def test_render_white_at_bottom_last_move_highlighted():
    b = chess.Board()
    b.push_san("e4")
    png = cg.render_board_png(b)
    # Just assert it renders without error for a non-trivial position.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_black_perspective_flips_the_image():
    b = chess.Board()
    white = cg.render_board_png(b)
    black = cg.render_board_png(b, perspective="black")
    assert white[:8] == b"\x89PNG\r\n\x1a\n"
    assert black[:8] == b"\x89PNG\r\n\x1a\n"
    # The two orientations must actually differ — the flip is real.
    assert white != black


def test_black_perspective_puts_black_back_rank_at_the_bottom():
    # For a player on black, h8 (black's king square) must sit at the
    # bottom-left, and a1 (white's rook square) at the top-right — the board
    # reads from black's side, not the standard white-at-the-bottom view.
    h8_x, h8_y = cg._pixel(chess.H8, perspective="black")
    a1_x, a1_y = cg._pixel(chess.A1, perspective="black")
    assert h8_x < 40 and h8_y > 460  # bottom-left
    assert a1_x > 460 and a1_y < 40  # top-right
    # Default (white) perspective: a1 stays bottom-left.
    a1w_x, a1w_y = cg._pixel(chess.A1)
    assert a1w_x < 40 and a1w_y > 460


def test_board_ascii_orientation():
    b = chess.Board()
    ascii_board = cg.board_ascii(b)
    # White back-rank (upper-case) must be the bottom line of the board.
    assert "R N B Q K B N R" in ascii_board
    # And rank labels present.
    assert "8 r n b q k b n r 8" in ascii_board


def test_choose_move_is_legal_and_sensible():
    b = chess.Board()
    move, san = cg.choose_bot_move(b, depth=3)
    assert move in b.legal_moves
    # Opening book: first moves are mainline, never a rook pawn push.
    assert san in {"e4", "d4", "Nf3", "c4"}


def test_choose_move_from_book_black():
    b = chess.Board()
    b.push_san("d4")
    move, san = cg.choose_bot_move(b, depth=3)
    assert move in b.legal_moves
    assert san in {"d5", "Nf6", "e6", "f5"}


def test_choose_move_midgame_legal():
    b = chess.Board()
    for s in ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]:
        b.push_san(s)
    move, _ = cg.choose_bot_move(b, depth=3)
    assert move in b.legal_moves


def test_parse_move_san_and_uci():
    g = cg.ChessGame(
        game_id="x",
        channel_id="c",
        player_id="p",
        player_name="p",
        bot_color=True,
        started_at="",
    )
    assert g.parse_move("e4").uci() == "e2e4"
    assert g.parse_move("e2e4").uci() == "e2e4"
    with pytest.raises(ValueError):
        g.parse_move("e5")  # illegal: black pawns can't move first


def test_apply_move_records_san_history():
    g = cg.ChessGame(
        game_id="x",
        channel_id="c",
        player_id="p",
        player_name="p",
        bot_color=True,
        started_at="",
    )
    g.apply_move(g.parse_move("e4"))
    assert g.history_san == ["e4"]
    assert g.turn == chess.BLACK


def test_manager_one_game_per_channel_and_owner(manager):
    game = manager.start("c1", "alice", "Alice", bot_color=True)
    assert manager.active("c1") is game
    # A second start in the same channel must NOT silently overwrite alice's
    # game — the one-game-per-channel rule protects the running game.
    with pytest.raises(ValueError):
        manager.start("c1", "bob", "Bob", bot_color=True)
    assert manager.active("c1").player_id == "alice"
    # Owner-only: alice owns the game; bob cannot take over.
    with pytest.raises(PermissionError):
        manager.game_for("c1", "bob")


def test_manager_persists_roundtrip(manager, tmp_path):
    game = manager.start("c9", "alice", "Alice", bot_color=True)
    g = manager.active("c9")
    assert g is not None
    # New manager reading the same file sees the same game.
    reloaded = cg.ChessManager(store_path=str(tmp_path / "chess_games.json"))
    again = reloaded.active("c9")
    assert again is not None
    assert again.fen == game.fen


def test_manager_resign_removes_game(manager):
    manager.start("c2", "alice", "Alice", bot_color=True)
    assert manager.active("c2") is not None
    manager.remove("c2")
    assert manager.active("c2") is None


def test_engine_beat_random_move_is_legal_through_game():
    import random

    random.seed(7)
    b = chess.Board()
    plies = 0
    while not b.is_game_over(claim_draw=True) and plies < 300:
        if b.turn == chess.WHITE:
            move, _ = cg.choose_bot_move(b, depth=2)
        else:
            move = random.choice(list(b.legal_moves))
        b.push(move)
        plies += 1
    assert plies < 300  # it terminated, no infinite loop


def test_endgame_detection():
    b = chess.Board("8/8/8/8/8/4k3/5P2/4K3 w - - 0 1")
    assert cg._endgame(b) is True
    b2 = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert cg._endgame(b2) is False


def _user(uid, *, name, display_name=None, nick=None):
    return SimpleNamespace(
        id=uid,
        name=name,
        display_name=display_name or name,
        global_name=display_name or name,
        nick=nick,
    )


def _msg(author, *, mentions=None, channel_id="c1", members=None):
    guild = SimpleNamespace(
        members=list(members or []),
        get_member=lambda uid: next(
            (m for m in (members or []) if int(m.id) == int(uid)), None
        ),
        get_user=lambda uid: next(
            (m for m in (members or []) if int(m.id) == int(uid)), None
        ),
        get_member_named=lambda n: next(
            (
                m
                for m in (members or [])
                if str(getattr(m, "name", "")).lower() == str(n).lower()
                or str(getattr(m, "display_name", "")).lower() == str(n).lower()
            ),
            None,
        ),
    )
    return SimpleNamespace(
        author=author,
        mentions=list(mentions or []),
        channel=SimpleNamespace(id=channel_id, guild=guild, recipients=[]),
        guild=guild,
    )


def test_chess_bot_name_uses_live_persona():
    import bot_tools

    assert bot_tools._chess_bot_name(None) == "Maxwell"
    assert bot_tools._chess_bot_name(SimpleNamespace(bot_name="Uni")) == "Uni"
    assert (
        bot_tools._chess_bot_name(
            SimpleNamespace(bot_name=None, user=SimpleNamespace(display_name="Max"))
        )
        == "Max"
    )


def test_chess_resolve_player_defaults_to_asker():
    import bot_tools

    alice = _user(1, name="alice", display_name="Alice")
    bot = _user(99, name="uni", display_name="Uni")
    msg = _msg(alice)
    uid, name = bot_tools._chess_resolve_player(
        msg, None, SimpleNamespace(user=bot, bot_name="Uni")
    )
    assert uid == "1"
    assert name == "Alice"


def test_chess_resolve_player_picks_the_mentioned_human():
    import bot_tools

    alice = _user(1, name="alice", display_name="Alice")
    bob = _user(2, name="bob", display_name="Bob")
    bot = _user(99, name="uni", display_name="Uni")
    msg = _msg(alice, mentions=[bot, bob])
    uid, name = bot_tools._chess_resolve_player(
        msg, None, SimpleNamespace(user=bot, bot_name="Uni")
    )
    assert uid == "2"
    assert name == "Bob"


def test_chess_resolve_player_explicit_name_and_mention():
    import bot_tools

    alice = _user(1, name="alice", display_name="Alice")
    bob = _user(2, name="bob", display_name="Bob")
    bot = _user(99, name="uni", display_name="Uni")
    host = SimpleNamespace(user=bot, bot_name="Uni")
    msg = _msg(alice, mentions=[bob], members=[alice, bob, bot])
    uid, name = bot_tools._chess_resolve_player(msg, "Bob", host)
    assert uid == "2" and name == "Bob"
    uid, name = bot_tools._chess_resolve_player(msg, "<@2>", host)
    assert uid == "2" and name == "Bob"
    uid, name = bot_tools._chess_resolve_player(msg, "2", host)
    assert uid == "2"


def test_chess_resolve_player_refuses_the_bot_as_opponent():
    import bot_tools
    import pytest

    alice = _user(1, name="alice", display_name="Alice")
    bot = _user(99, name="uni", display_name="Uni")
    host = SimpleNamespace(user=bot, bot_name="Uni")
    msg = _msg(alice, mentions=[bot], members=[alice, bot])
    with pytest.raises(ValueError, match="cannot play against Uni"):
        bot_tools._chess_resolve_player(msg, "Uni", host)


def test_chess_state_text_and_resign_use_bot_name():
    import bot_tools

    g = cg.ChessGame(
        game_id="x",
        channel_id="c",
        player_id="p",
        player_name="Alice",
        bot_color=True,
        started_at="",
    )
    text = bot_tools._chess_state_text(g, bot_name="Uni")
    assert "Uni=white" in text
    assert "Maxwell=" not in text
    assert "It is Uni's move." in text
    host = SimpleNamespace(bot_name="Uni")
    assert bot_tools._chess_is_bot_resign("uni", host)
    assert bot_tools._chess_is_bot_resign("bot", host)
    assert not bot_tools._chess_is_bot_resign("maxwell", host)
    assert bot_tools._chess_is_bot_resign(
        "maxwell", SimpleNamespace(bot_name="Maxwell")
    )


def test_chess_start_description_uses_bot_name():
    import bot_tools

    tool = bot_tools.ChessStartTool(SimpleNamespace(bot_name="Uni"))
    desc = tool.get_description()
    assert "Uni" in desc
    assert "Maxwell" not in desc
    assert "opponent=" in desc


# --------------------------------------------------------------------------
# Maxwell picks his own moves. The search stays only as a wedge-breaker, so
# what matters is that the position he is handed is actually playable from.
# --------------------------------------------------------------------------


def test_annotated_moves_name_what_a_capture_takes():
    b = chess.Board()
    b.push_san("e4")
    b.push_san("d5")
    ann = cg.annotate_legal_moves(b)
    exd5 = next(m for m in ann if m.startswith("exd5"))
    assert "takes pawn" in exd5


def test_annotated_moves_flag_a_piece_that_just_gets_taken():
    # Bishop to b5 is met by the knight on d4; nothing defends b5.
    b = chess.Board("r1bqkbnr/pppp1ppp/8/4p3/2BnP3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    ann = cg.annotate_legal_moves(b)
    bb5 = next(m for m in ann if m.startswith("Bb5"))
    assert "LOSES THE PIECE" in bb5


def test_annotated_moves_do_not_cry_wolf_on_a_defended_square():
    b = chess.Board("r1bqkbnr/pppp1ppp/8/4p3/2BnP3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    ann = cg.annotate_legal_moves(b)
    bb3 = next(m for m in ann if m.startswith("Bb3"))
    assert "defended" in bb3
    assert "LOSES THE PIECE" not in bb3


def test_annotated_moves_mark_an_even_trade_as_a_trade():
    b = chess.Board("r1bqkbnr/pppp1ppp/8/4p3/2BnP3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    ann = cg.annotate_legal_moves(b)
    nxd4 = next(m for m in ann if m.startswith("Nxd4"))
    assert "takes knight" in nxd4
    assert "LOSES THE PIECE" not in nxd4


def test_annotated_moves_shout_about_mate():
    # Qh5 mates on the back-rank-ish scholar pattern.
    b = chess.Board(
        "r1bqkbnr/pppp1ppp/2n5/2b1p3/4P3/5Q2/PPPP1PPP/RNB1KBNR w KQkq - 0 1"
    )
    b.push_san("Qxf7+")
    b.pop()
    ann = cg.annotate_legal_moves(b)
    qxf7 = next(m for m in ann if m.startswith("Qxf7"))
    assert "CHECKMATE" in qxf7 or "check" in qxf7


def test_annotated_moves_cover_every_legal_move():
    b = chess.Board()
    assert len(cg.annotate_legal_moves(b)) == len(list(b.legal_moves))


def test_annotating_does_not_disturb_the_board():
    b = chess.Board()
    b.push_san("e4")
    before = b.fen()
    cg.annotate_legal_moves(b)
    cg.position_notes(b)
    assert b.fen() == before


def test_position_notes_call_out_free_material():
    b = chess.Board("r1bqkbnr/pppp1ppp/8/4p3/2BnP3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    notes = " ".join(cg.position_notes(b))
    assert "Free material" in notes
    assert "Nxe5" in notes


def test_position_notes_announce_check():
    b = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    assert b.is_check()
    notes = " ".join(cg.position_notes(b))
    assert "IN CHECK" in notes


def test_position_notes_report_being_down_material():
    # White is a whole queen short.
    b = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB1KBNR w KQkq - 0 1")
    notes = " ".join(cg.position_notes(b))
    assert "-900" in notes


def test_state_text_hands_maxwell_the_full_annotated_move_list():
    """On his own turn he must see every move — a truncated list hides the
    one move that does not lose."""
    import bot_tools

    g = cg.ChessGame(
        game_id="x",
        channel_id="c",
        player_id="p",
        player_name="Alice",
        bot_color=True,
        started_at="",
    )
    text = bot_tools._chess_state_text(g, bot_name="Uni")
    assert "YOUR LEGAL MOVES (20)" in text
    assert "…" not in text  # no truncation on his own turn
    assert "chess_move(move=" in text
    assert "Play to win." in text


def test_state_text_truncates_only_the_opponents_list():
    import bot_tools

    g = cg.ChessGame(
        game_id="x",
        channel_id="c",
        player_id="p",
        player_name="Alice",
        bot_color=False,  # Maxwell is black, so white/Alice is to move
        started_at="",
    )
    text = bot_tools._chess_state_text(g, bot_name="Uni")
    assert "Legal moves for them (20)" in text
    assert "YOUR LEGAL MOVES" not in text


def test_state_text_stops_at_the_result_when_the_game_is_over():
    import bot_tools

    g = cg.ChessGame(
        game_id="x",
        channel_id="c",
        player_id="p",
        player_name="Alice",
        bot_color=True,
        started_at="",
    )
    # Fool's mate: black delivers Qh4#.
    for san in ("f3", "e5", "g4", "Qh4#"):
        g.apply_move(g.parse_move(san))
    text = bot_tools._chess_state_text(g, bot_name="Uni")
    assert "GAME OVER" in text
    assert "YOUR LEGAL MOVES" not in text


def test_move_tool_description_says_maxwell_chooses():
    import bot_tools

    desc = bot_tools.ChessMoveTool(SimpleNamespace(bot_name="Uni")).get_description()
    assert "YOU choose" in desc
    assert "no engine playing for you" in desc


def test_miss_counter_escalates_then_resets():
    import bot_tools

    bot_tools._chess_clear_misses("g1")
    assert bot_tools._chess_miss_count("g1") == 0
    assert bot_tools._chess_note_miss("g1") == 1
    assert bot_tools._chess_note_miss("g1") == 2
    assert bot_tools._chess_miss_count("g1") == 2
    bot_tools._chess_clear_misses("g1")
    assert bot_tools._chess_miss_count("g1") == 0


def test_miss_counter_is_per_game():
    import bot_tools

    bot_tools._chess_clear_misses("a")
    bot_tools._chess_clear_misses("b")
    bot_tools._chess_note_miss("a")
    assert bot_tools._chess_miss_count("b") == 0


def test_fallback_search_still_returns_a_legal_move():
    """The wedge-breaker has to work, or a stuck game stays stuck."""
    b = chess.Board("r1bqkbnr/pppp1ppp/8/4p3/2BnP3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    move, san = cg.choose_bot_move(b, depth=2)
    assert move in b.legal_moves
    assert san
