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


def test_board_rendering():
    game = CheckersGame()
    png_data = game.render_board_png()
    assert len(png_data) > 1000
    assert png_data.startswith(b"\x89PNG")
