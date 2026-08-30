"""
Unit tests for Checkers game engine.
"""
from plugins.checkers.checkers_game import (
    CheckersGame,
    RED_MAN,
    BLACK_MAN,
    RED_KING,
    coord_to_square,
    square_to_coord,
)


def test_square_coords():
    assert square_to_coord("a8") == (0, 0)
    assert square_to_coord("h1") == (7, 7)
    assert square_to_coord("c3") == (5, 2)
    assert coord_to_square(5, 2) == "c3"


def test_initial_board_and_legal_moves():
    game = CheckersGame()
    assert game.turn == "red"
    legal_moves = game.get_legal_moves("red")
    # In initial board, 4 red pieces can make simple moves forward (7 moves total)
    assert len(legal_moves) > 0

    # Execute a move: c3-d4 is (5, 2) to (4, 3)
    ok, msg = game.make_move("c3-d4")
    assert ok
    assert game.turn == "black"


def test_jump_capture_and_kinging():
    game = CheckersGame()
    # Clear board
    for r in range(8):
        for c in range(8):
            game.board[r][c] = 0

    # Place red man on c3 (5, 2) and black man on d4 (4, 3)
    game.board[5][2] = RED_MAN
    game.board[4][3] = BLACK_MAN

    jumps = game.get_jumps_for_piece(5, 2)
    assert len(jumps) == 1
    assert jumps[0] == (5, 2, 3, 4)  # c3 to e5

    # Make jump
    ok, msg = game.make_move("c3-e5")
    assert ok
    # Black piece at d4 (4,3) should be captured (0)
    assert game.board[4][3] == 0
    assert game.board[3][4] == RED_MAN


def test_multi_jump_stays_with_capturing_piece():
    game = CheckersGame()
    for r in range(8):
        for c in range(8):
            game.board[r][c] = 0
    game.board[5][2] = RED_MAN  # c3
    game.board[5][0] = RED_MAN  # a3, another red piece
    game.board[4][3] = BLACK_MAN  # d4
    game.board[2][5] = BLACK_MAN  # f6

    ok, _ = game.make_move("c3-e5")
    assert ok
    assert game.turn == "red"
    assert game.get_legal_moves() == [(3, 4, 1, 6)]

    other_ok, _ = game.make_move("a3-b4")
    assert not other_ok
    assert game.turn == "red"

    ok, _ = game.make_move("e5-g7")
    assert ok
    assert game.turn == "black"


def test_promotion_ends_a_jump_chain():
    game = CheckersGame()
    for r in range(8):
        for c in range(8):
            game.board[r][c] = 0
    game.board[2][1] = RED_MAN  # b6
    game.board[1][2] = BLACK_MAN  # c7
    game.board[1][4] = BLACK_MAN  # e7, king jump would be available

    ok, _ = game.make_move("b6-d8")
    assert ok
    assert game.board[0][3] == RED_KING
    assert game.turn == "black"
    assert game._forced_piece is None


def test_board_rendering():
    game = CheckersGame()
    png_data = game.render_board_png()
    assert len(png_data) > 1000
    assert png_data.startswith(b"\x89PNG")
