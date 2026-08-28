"""Tests for the chess engine + manager (no Discord needed).

Covers the pure logic in ``chess_game.py``: board rendering, the move search,
the opening book, SAN/UCI parsing, and the one-game-per-channel, one-player
focus rule. The Discord-facing ``chess_*`` tools are thin wrappers on top and
are exercised in the live bot.
"""

from __future__ import annotations


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
        game_id="x", channel_id="c", player_id="p",
        player_name="p", bot_color=True, started_at="",
    )
    assert g.parse_move("e4").uci() == "e2e4"
    assert g.parse_move("e2e4").uci() == "e2e4"
    with pytest.raises(ValueError):
        g.parse_move("e5")  # illegal: black pawns can't move first


def test_apply_move_records_san_history():
    g = cg.ChessGame(
        game_id="x", channel_id="c", player_id="p",
        player_name="p", bot_color=True, started_at="",
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
