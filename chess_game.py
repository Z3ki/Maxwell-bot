"""Chess game engine + board rendering.

Owns the board state for one (channel, player) chess game, renders the board
to a PNG (Pillow + the DejaVu chess glyphs), picks the bot's moves with a
small alpha-beta search, and persists games to ``data/chess_games.json`` so a
restart does not wipe an in-progress game.

Pure engine + serialization: no Discord types here, so it is unit-testable
without the bot. The ``chess_*`` tools in ``bot_tools.py`` are the thin
Discord layer on top.
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import chess

try:  # Pillow is a hard dep elsewhere (image_tools), but keep this module
    # importable without it so unit tests that only exercise the engine work.
    from PIL import Image, ImageDraw, ImageFont

    _PIL = True
except Exception:  # pragma: no cover - extremely unlikely
    _PIL = False

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Board rendering
# --------------------------------------------------------------------------- #

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]

# Filled glyphs (U+265A..U+265F) used for BOTH colours, tinted per side so the
# pieces read solid on any square. Outline glyphs (U+2654..U+2659) render as
# thin line-art and disappear on the busy squares; the filled set + a 2px
# border is far more legible.
_GLYPH = {
    chess.WHITE: {
        chess.KING: "\u265a",
        chess.QUEEN: "\u265b",
        chess.ROOK: "\u265c",
        chess.BISHOP: "\u265d",
        chess.KNIGHT: "\u265e",
        chess.PAWN: "\u265f",
    },
    chess.BLACK: {
        chess.KING: "\u265a",
        chess.QUEEN: "\u265b",
        chess.ROOK: "\u265c",
        chess.BISHOP: "\u265d",
        chess.KNIGHT: "\u265e",
        chess.PAWN: "\u265f",
    },
}

_LIGHT_SQ = (240, 217, 181, 255)
_DARK_SQ = (181, 136, 99, 255)
_WHITE_PIECE = (248, 248, 248, 255)
_BLACK_PIECE = (38, 38, 38, 255)
_WHITE_OUT = (28, 28, 28, 255)
_BLACK_OUT = (235, 228, 210, 255)
_COORD = (70, 70, 70, 255)
_LAST_MOVE = (255, 213, 79, 150)
_CHECK = (232, 62, 62, 150)

_SQUARE = 64
_MARGIN = 34
_SIZE = 8 * _SQUARE + 2 * _MARGIN


def _load_font(size: int) -> object:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception as e:
                logger.debug("Font %s unusable: %s", path, e)
                continue
    return ImageFont.load_default()


def _pixel(
    square: int, *, margin: int = _MARGIN, perspective: str = "white"
) -> tuple[int, int]:
    """Pixel coords for a square.

    Default ``perspective="white"`` orients white-at-the-bottom (a1 bottom-left).
    ``perspective="black"`` rotates 180° so black is at the bottom (h8 bottom-left),
    which is how a player on black sees the board.
    """
    if perspective == "black":
        # 180° rotation: file h at the left, rank 8 at the bottom.
        return (
            margin + (7 - chess.square_file(square)) * _SQUARE,
            margin + chess.square_rank(square) * _SQUARE,
        )
    return (
        margin + chess.square_file(square) * _SQUARE,
        margin + (7 - chess.square_rank(square)) * _SQUARE,
    )


def render_board_png(board: chess.Board, perspective: str = "white") -> bytes:
    """Render ``board`` to PNG bytes.

    Default ``perspective="white"`` puts white at the bottom; ``"black"`` puts
    black at the bottom (180° rotation, coordinates flipped to match). Last move
    and the king in check are highlighted in both.
    """
    if not _PIL:
        raise RuntimeError("Pillow is not available; cannot render the board")
    img = Image.new("RGBA", (_SIZE, _SIZE), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_big = _load_font(int(_SQUARE * 0.82))
    font_small = _load_font(int(_MARGIN * 0.58))
    perspective = perspective if perspective == "black" else "white"

    def px(sq: int) -> tuple[int, int]:
        return _pixel(sq, perspective=perspective)

    # Board squares.
    for sq in chess.SQUARES:
        x, y = px(sq)
        shade = _LIGHT_SQ if (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 0 else _DARK_SQ
        draw.rectangle([x, y, x + _SQUARE, y + _SQUARE], fill=shade)

    # Last move squares.
    if board.move_stack:
        try:
            last = board.peek()
            for sq in (last.from_square, last.to_square):
                x, y = px(sq)
                draw.rectangle(
                    [x, y, x + _SQUARE, y + _SQUARE],
                    fill=_LAST_MOVE,
                )
        except Exception as e:
            # Cosmetic highlight only — still render the board.
            logger.debug("Could not highlight last move: %s", e)

    # King in check.
    if board.is_check():
        king_sq = board.king(board.turn)
        if king_sq is not None:
            x, y = px(king_sq)
            draw.rectangle([x, y, x + _SQUARE, y + _SQUARE], fill=_CHECK)

    # Pieces.
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None:
            continue
        glyph = _GLYPH[piece.color][piece.piece_type]
        cx, cy = px(sq)
        cx += _SQUARE // 2
        cy += _SQUARE // 2
        fill = _WHITE_PIECE if piece.color == chess.WHITE else _BLACK_PIECE
        outline = _WHITE_OUT if piece.color == chess.WHITE else _BLACK_OUT
        for dx, dy in (
            (-2, 0), (2, 0), (0, -2), (0, 2),
            (-2, -2), (2, 2), (-2, 2), (2, -2),
        ):
            draw.text((cx + dx, cy + dy), glyph, font=font_big, fill=outline, anchor="mm")
        draw.text((cx, cy), glyph, font=font_big, fill=fill, anchor="mm")

    # Coordinates. A black-perspective board reads files h→a and ranks 1→8 so
    # the labels match the flipped board instead of contradicting it.
    file_labels = "hgfedcba" if perspective == "black" else "abcdefgh"
    for f in range(8):
        label = file_labels[f]
        draw.text(
            (_MARGIN + f * _SQUARE + _SQUARE // 2, _MARGIN // 2),
            label,
            font=font_small,
            fill=_COORD,
            anchor="mm",
        )
        draw.text(
            (_MARGIN + f * _SQUARE + _SQUARE // 2, _SIZE - _MARGIN // 2),
            label,
            font=font_small,
            fill=_COORD,
            anchor="mm",
        )
    for r in range(8):
        rank_label = str(r + 1) if perspective == "black" else str(8 - r)
        draw.text(
            (_MARGIN // 2, _MARGIN + r * _SQUARE + _SQUARE // 2),
            rank_label,
            font=font_small,
            fill=_COORD,
            anchor="mm",
        )
        draw.text(
            (_SIZE - _MARGIN // 2, _MARGIN + r * _SQUARE + _SQUARE // 2),
            rank_label,
            font=font_small,
            fill=_COORD,
            anchor="mm",
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def board_ascii(board: chess.Board) -> str:
    """A compact text board the model can read in a tool result."""
    lines: list[str] = ["  a b c d e f g h"]
    for rank in range(7, -1, -1):
        row: list[str] = [str(rank + 1)]
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            if piece is None:
                row.append(".")
            else:
                symbol = piece.symbol()
                row.append(symbol.upper() if piece.color == chess.WHITE else symbol.lower())
        row.append(str(rank + 1))
        lines.append(" ".join(row))
    lines.append("  a b c d e f g h")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

_VAL = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}

# Indexed [piece_type][0..63] with rank 8 = row 0 (standard "simplified" PSTs).
_PST = {
    chess.PAWN: [
        0, 0, 0, 0, 0, 0, 0, 0,
        50, 50, 50, 50, 50, 50, 50, 50,
        10, 10, 20, 30, 30, 20, 10, 10,
        5, 5, 10, 25, 25, 10, 5, 5,
        0, 0, 0, 20, 20, 0, 0, 0,
        5, -5, -10, 0, 0, -10, -5, 5,
        5, 10, 10, -20, -20, 10, 10, 5,
        0, 0, 0, 0, 0, 0, 0, 0,
    ],
    chess.KNIGHT: [
        -50, -40, -30, -30, -30, -30, -40, -50,
        -40, -20, 0, 0, 0, 0, -20, -40,
        -30, 0, 10, 15, 15, 10, 0, -30,
        -30, 5, 15, 20, 20, 15, 5, -30,
        -30, 0, 15, 20, 20, 15, 0, -30,
        -30, 5, 10, 15, 15, 10, 5, -30,
        -40, -20, 0, 5, 5, 0, -20, -40,
        -50, -40, -30, -30, -30, -30, -40, -50,
    ],
    chess.BISHOP: [
        -20, -10, -10, -10, -10, -10, -10, -20,
        -10, 0, 0, 0, 0, 0, 0, -10,
        -10, 0, 5, 10, 10, 5, 0, -10,
        -10, 5, 5, 10, 10, 5, 5, -10,
        -10, 0, 10, 10, 10, 10, 0, -10,
        -10, 10, 10, 10, 10, 10, 10, -10,
        -10, 5, 0, 0, 0, 0, 5, -10,
        -20, -10, -10, -10, -10, -10, -10, -20,
    ],
    chess.ROOK: [
        0, 0, 0, 0, 0, 0, 0, 0,
        5, 10, 10, 10, 10, 10, 10, 5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        -5, 0, 0, 0, 0, 0, 0, -5,
        0, 0, 0, 5, 5, 0, 0, 0,
    ],
    chess.QUEEN: [
        -20, -10, -10, -5, -5, -10, -10, -20,
        -10, 0, 0, 0, 0, 0, 0, -10,
        -10, 0, 5, 5, 5, 5, 0, -10,
        -5, 0, 5, 5, 5, 5, 0, -5,
        0, 0, 5, 5, 5, 5, 0, -5,
        -10, 5, 5, 5, 5, 5, 0, -10,
        -10, 0, 5, 0, 0, 0, 0, -10,
        -20, -10, -10, -5, -5, -10, -10, -20,
    ],
    chess.KING: [
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -20, -30, -30, -40, -40, -30, -30, -20,
        -10, -20, -20, -20, -20, -20, -20, -10,
        20, 20, 0, 0, 0, 0, 20, 20,
        20, 30, 10, 0, 0, 10, 30, 20,
    ],
}

# King PST used once only the endgame is reached (few pieces left).
_KING_ENDGAME = [
    -50, -40, -30, -20, -20, -30, -40, -50,
    -30, -20, -10, 0, 0, -10, -20, -30,
    -30, -10, 20, 30, 30, 20, -10, -30,
    -30, -10, 30, 40, 40, 30, -10, -30,
    -30, -10, 30, 40, 40, 30, -10, -30,
    -30, -10, 20, 30, 30, 20, -10, -30,
    -30, -30, 0, 0, 0, 0, -30, -30,
    -50, -30, -30, -30, -30, -30, -30, -50,
]

_MATE = 1_000_000


def _pst_index(square: int, color: chess.Color) -> int:
    """Index into a rank-8-first PST table for a piece on ``square``.

    White reads the table top-down (rank 8 = row 0); black mirrors the board
    vertically so the same tables apply symmetrically.
    """
    if color == chess.WHITE:
        rank_idx = chess.square_rank(square)
        file = chess.square_file(square)
        return (7 - rank_idx) * 8 + file
    # Black: mirror so a black piece on its 7th rank reads like a white piece.
    mirrored = square ^ 56
    rank_idx = chess.square_rank(mirrored)
    file = chess.square_file(mirrored)
    return (7 - rank_idx) * 8 + file


def _endgame(board: chess.Board) -> bool:
    """True when the position is an endgame (king is an active piece).

    A heuristic: once queens are gone and material is low, the king should
    step forward (independent of the middlegame king-safety table). Any position
    with few enough pieces counts too, so K+P vs K reads as an endgame.
    """
    pieces = board.piece_map()
    queens = sum(1 for p in pieces.values() if p.piece_type == chess.QUEEN)
    material = len(pieces)
    # Queens still on the board mean middlegame orchestration, not an endgame
    # — even a KQ vs K mate is guided better by the middlegame king table.
    if queens and material > 6:
        return False
    return material <= 16


def _evaluate(board: chess.Board) -> float:
    """Static eval from the side-to-move's perspective (negamax convention)."""
    if board.is_checkmate():
        return -_MATE + board.fullmove_number
    if board.is_stalemate() or board.is_insufficient_material():
        return 0.0
    if board.is_game_over(claim_draw=True) and not board.is_checkmate():
        return 0.0

    eg = _endgame(board)
    score = 0.0
    for color in (chess.WHITE, chess.BLACK):
        sign = 1.0 if color == chess.WHITE else -1.0
        for ptype, pst in _PST.items():
            for sq in board.pieces(ptype, color):
                idx = _pst_index(sq, color)
                table = _KING_ENDGAME if (ptype == chess.KING and eg) else pst
                score += sign * (_VAL[ptype] + table[idx])
    return score if board.turn == chess.WHITE else -score


def _move_value(board: chess.Board, move: chess.Move) -> int:
    """MVV-LVA-ish ordering heuristic (higher = searched first)."""
    score = 0
    if board.is_capture(move):
        victim = board.piece_at(move.to_square)
        if victim is not None:
            attacker = board.piece_at(move.from_square)
            attacker_val = _VAL[attacker.piece_type] if attacker is not None else 0
            score += 10 * _VAL[victim.piece_type] - attacker_val
    if move.promotion:
        score += _VAL[move.promotion]
    return -score  # sort descending -> negate


def _search(board: chess.Board, depth: int, alpha: float, beta: float) -> float:
    if depth == 0:
        return _evaluate(board)
    if board.is_checkmate():
        return -_MATE + board.fullmove_number
    if board.is_stalemate() or board.is_insufficient_material():
        return 0.0
    if board.is_game_over(claim_draw=True) and not board.is_checkmate():
        return 0.0

    moves = sorted(board.legal_moves, key=lambda m: _move_value(board, m))
    best = -float("inf")
    for move in moves:
        board.push(move)
        value = -_search(board, depth - 1, -beta, -alpha)
        board.pop()
        if value > best:
            best = value
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def choose_bot_move(
    board: chess.Board, depth: int = 3, *, jitter: float = 0.0
) -> tuple[chess.Move, str]:
    """Pick the bot's move. Returns ``(move, san)``.

    ``jitter`` in [0,1] adds strength-varying randomness among near-equal moves
    so opening play is not sterile. ``depth<=0`` or no legal moves -> random.
    """
    moves = list(board.legal_moves)
    if not moves:
        raise chess.IllegalMoveError("no legal moves")

    # Opening book first — the engine's flat opening eval would otherwise let
    # a random tie-break pick 1.a3. Off-book (or past the booked lines) the
    # search takes over.
    book_move = _opening_move(board)
    if book_move is not None and book_move in board.legal_moves:
        return book_move, board.san(book_move)

    if depth <= 0:
        move = random.choice(moves)
        return move, board.san(move)

    ordered = sorted(moves, key=lambda m: _move_value(board, m))
    scored: list[tuple[float, chess.Move]] = []
    alpha = -float("inf")
    beta = float("inf")
    for move in ordered:
        board.push(move)
        value = -_search(board, depth - 1, -beta, -alpha)
        board.pop()
        scored.append((value, move))
        if value > alpha:
            alpha = value

    scored.sort(key=lambda t: t[0], reverse=True)
    best_value = scored[0][0] if scored else 0.0
    if jitter > 0 and len(scored) > 1:
        tie_band = max(20, abs(best_value) * 0.05)
        pool = [(v, m) for v, m in scored if best_value - v <= tie_band]
        if len(pool) > 1:
            # Weighted toward the best but not deterministic.
            weights = [1.0 / (1.0 + (best_value - v) / max(1.0, abs(best_value) + 1)) for v, _ in pool]
            chosen = _weighted_choice(pool, weights)
        else:
            chosen = pool[0]
        move = chosen[1]
    else:
        move = scored[0][1]
    return move, board.san(move)


def _weighted_choice(pool: list, weights: list[float]):
    total = sum(weights) or 1.0
    r = random.uniform(0.0, total)
    acc = 0.0
    # strict=True: a weights list shorter than the pool would silently drop
    # candidate moves instead of failing, which is invisible in a game log.
    for item, weight in zip(pool, weights, strict=True):
        acc += weight
        if r <= acc:
            return item
    return pool[-1]


# A tiny opening book. The engine's flat opening eval makes too many first
# moves look equal, so a random tie-break would happily play 1.a3. Keyed by
# the SAN history so far; only used for the first handful of plies, then the
# search takes over. Values are the moves the book side would play next.
_OPENING_BOOK: dict[tuple[str, ...], list[str]] = {
    (): ["e4", "d4", "Nf3", "c4"],
    ("e4",): ["e5", "c5", "e6", "c6", "d6"],
    ("d4",): ["d5", "Nf6", "e6", "f5"],
    ("Nf3",): ["d5", "Nf6", "c5", "g6"],
    ("c4",): ["e5", "c5", "Nf6", "e6", "g6"],
    # Open / double king pawn
    ("e4", "e5"): ["Nf3", "Nc3", "Bc4"],
    ("e4", "e5", "Nf3"): ["Nc6", "Nf6"],
    ("e4", "e5", "Nf3", "Nc6"): ["Bb5", "Bc4", "d4", "Nc3"],  # Ruy/Italian/Scotch
    ("e4", "e5", "Nf3", "Nf6"): ["Nxe5", "Nc3"],
    # Sicilian
    ("e4", "c5"): ["Nf3", "Nc3", "c3", "d4"],
    ("e4", "c5", "Nf3"): ["d6", "Nc6", "e6"],
    ("e4", "c5", "Nf3", "d6"): ["d4", "Bb5+"],
    ("e4", "c5", "Nf3", "Nc6"): ["d4", "Bb5"],
    ("e4", "c5", "Nf3", "d6", "d4"): ["cxd4", "Nf6"],
    # French / Caro
    ("e4", "e6"): ["d4", "Nf3"],
    ("e4", "e6", "d4"): ["d5", "c5"],
    ("e4", "c6"): ["d4", "Nc3", "c4"],
    ("e4", "c6", "d4"): ["d5", "Nf6"],
    # Queen's gambit
    ("d4", "d5"): ["c4", "Nf3"],
    ("d4", "d5", "c4"): ["e6", "c6", "dxc4"],
    ("d4", "d5", "c4", "e6"): ["Nc3", "Nf3"],
    ("d4", "Nf6"): ["c4", "Nf3", "g3"],
    ("d4", "Nf6", "c4"): ["g6", "e6", "c5"],
    ("d4", "Nf6", "c4", "g6"): ["Nc3", "g3", "e4"],  # KID lines
    ("d4", "Nf6", "c4", "e6"): ["Nc3", "Nf3", "g3"],
    ("d4", "f5"): ["g3", "Nf3", "c4"],  # Dutch
    # English
    ("c4", "e5"): ["Nc3", "g3", "Nf3"],
    ("c4", "e5", "Nc3"): ["Nf6", "Nc6"],
    ("c4", "c5"): ["Nf3", "Nc3", "g3"],
    ("c4", "Nf6"): ["Nc3", "Nf3", "g3"],
    # Reti / King's Indian
    ("Nf3", "d5"): ["c4", "d4", "g3"],
    ("Nf3", "Nf6"): ["c4", "g3", "d4"],
    ("Nf3", "d5", "c4"): ["e6", "c6", "dxc4"],
}


def _history_san(board: chess.Board) -> list[str]:
    """SAN for every move already on the board, in order.

    ``board.san()`` fails on old moves (they are no longer legal in the current
    position), so replay a scratch board, capturing SAN as each move is made.
    """
    scratch = chess.Board()
    history: list[str] = []
    for move in board.move_stack:
        try:
            history.append(scratch.san(move))
            scratch.push(move)
        except Exception:  # pragma: no cover - corrupt/invalid stack
            break
    return history


def _opening_move(board: chess.Board) -> chess.Move | None:
    """A book move for the current position, or None when off-book/too deep."""
    history = tuple(_history_san(board))
    candidates = _OPENING_BOOK.get(history)
    if not candidates:
        return None
    legal: list[chess.Move] = []
    for san in candidates:
        try:
            move = board.parse_san(san)
        except Exception as e:
            # Book entry that doesn't apply to this position.
            logger.debug("Book move %s unparseable here: %s", san, e)
            continue
        if move in board.legal_moves:
            legal.append(move)
    if not legal:
        return None
    return random.choice(legal)


# --------------------------------------------------------------------------- #
# Game state
# --------------------------------------------------------------------------- #

class ChessGame:
    """A single game of chess between this bot and one Discord user in a channel."""

    def __init__(
        self,
        *,
        game_id: str,
        channel_id: str,
        player_id: str,
        player_name: str,
        bot_color: chess.Color,
        started_at: str,
        max_depth: int = 3,
        jitter: float = 0.0,
    ) -> None:
        self.game_id = game_id
        self.channel_id = channel_id
        self.player_id = player_id
        self.player_name = player_name
        self.bot_color = bot_color
        self.started_at = started_at
        self.max_depth = max_depth
        self.jitter = jitter
        self.board = chess.Board()
        # SAN moves as played. Stored at push time (not recomputed from the
        # board), because board.san() on an already-made move fails — old
        # moves are not legal in the current position.
        self.history: list[str] = []

    # -- convenience ------------------------------------------------------- #
    @property
    def player_color(self) -> chess.Color:
        return not self.bot_color

    @property
    def turn(self) -> chess.Color:
        return self.board.turn

    @property
    def turn_label(self) -> str:
        return "white" if self.board.turn == chess.WHITE else "black"

    @property
    def bot_turn(self) -> bool:
        return self.board.turn == self.bot_color

    @property
    def legal_san(self) -> list[str]:
        return [self.board.san(move) for move in self.board.legal_moves]

    @property
    def legal_uci(self) -> list[str]:
        return [move.uci() for move in self.board.legal_moves]

    @property
    def fen(self) -> str:
        return self.board.fen()

    @property
    def history_san(self) -> list[str]:
        return list(self.history)

    @property
    def is_over(self) -> bool:
        return self.board.is_game_over(claim_draw=True)

    @property
    def result(self) -> str | None:
        """Human-readable result, or None while still playing."""
        if not self.board.is_game_over(claim_draw=True):
            return None
        if self.board.is_checkmate():
            winner = "black" if self.board.turn == chess.WHITE else "white"
            return f"checkmate — {winner} wins"
        if self.board.is_stalemate():
            return "draw by stalemate"
        if self.board.is_insufficient_material():
            return "draw by insufficient material"
        if self.board.is_repetition():
            return "draw by repetition"
        if self.board.is_fifty_moves():
            return "draw by fifty-move rule"
        return "draw"

    # -- serialization ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "channel_id": self.channel_id,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "bot_color": self.bot_color,
            "started_at": self.started_at,
            "max_depth": self.max_depth,
            "jitter": self.jitter,
            "fen": self.board.fen(),
            "history_san": self.history_san,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChessGame":
        game = cls(
            game_id=data.get("game_id", ""),
            channel_id=data.get("channel_id", ""),
            player_id=data.get("player_id", ""),
            player_name=data.get("player_name", "player"),
            bot_color=bool(data.get("bot_color", True)),
            started_at=data.get("started_at", ""),
            max_depth=int(data.get("max_depth", 3)),
            jitter=float(data.get("jitter", 0.0)),
        )
        fen = data.get("fen", "")
        game.history = list(data.get("history_san", []) or [])
        if fen:
            game.board.set_fen(fen)
        else:
            # No FEN but a move list: replay it.
            for san in data.get("history_san", []) or []:
                try:
                    game.board.push_san(san)
                except Exception:
                    break
        return game

    # -- moves ------------------------------------------------------------- #
    def parse_move(self, move_text: str) -> chess.Move:
        """Accept SAN (e4, Nf3, O-O, exd5) or UCI (e2e4, e7e8q). Raises
        ``ValueError`` when illegal or unparseable."""
        text = move_text.strip().replace(" ", "")
        if not text:
            raise ValueError("move is empty")
        # UCI: exactly 4 or 5 chars of [a-h][1-8][a-h][1-8]([nbrq]) or castle.
        uci = None
        try:
            uci = chess.Move.from_uci(text)
        except Exception:
            # Not UCI — the SAN parse below is the normal path.
            uci = None
        if uci is not None:
            if uci in self.board.legal_moves:
                return uci
            raise ValueError(f"{text} is not a legal move right now")
        try:
            return self.board.parse_san(text)
        except Exception as e:
            # Fall through to the sloppy-SAN retry below.
            logger.debug("SAN parse failed for %r: %s", text, e)
        # Uppercase the rank/file pairs to help sloppy SAN.
        upper = text.upper()
        try:
            return self.board.parse_san(upper)
        except Exception as exc:
            raise ValueError(
                f"'{move_text}' is not a legal move. Legal moves: "
                + ", ".join(self.legal_san)
            ) from exc

    def apply_move(self, move: chess.Move) -> str:
        """Push a legal move and return its SAN."""
        san = self.board.san(move)
        self.board.push(move)
        self.history.append(san)
        return san


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #

def _data_dir() -> str:
    env = os.environ.get("MAXWELL_DATA_DIR", "")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _store_path() -> str:
    return os.path.join(_data_dir(), "chess_games.json")


class ChessManager:
    """Holds and persists one active game per Discord channel."""

    def __init__(self, *, store_path: str | None = None) -> None:
        self._store = store_path or _store_path()
        self._games: dict[str, ChessGame] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        try:
            with open(self._store, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        loaded: dict[str, ChessGame] = {}
        for channel_id, data in (raw or {}).items():
            try:
                game = ChessGame.from_dict(data)
                if not game.is_over:
                    loaded[str(channel_id)] = game
            except Exception as exc:  # pragma: no cover - corrupt row
                logger.warning("Dropping corrupt chess game %s: %s", channel_id, exc)
        self._games = loaded

    def _save(self) -> None:
        payload = {chan: game.to_dict() for chan, game in self._games.items()}
        try:
            os.makedirs(os.path.dirname(self._store), exist_ok=True)
            tmp = self._store + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._store)
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not persist chess store: %s", exc)

    # -- queries ----------------------------------------------------------- #
    def active(self, channel_id: str) -> ChessGame | None:
        return self._games.get(str(channel_id))

    # -- mutations --------------------------------------------------------- #
    def start(
        self,
        channel_id: str,
        player_id: str,
        player_name: str,
        *,
        bot_color: chess.Color | None = None,
        max_depth: int = 3,
        jitter: float = 0.0,
        force: bool = False,
    ) -> ChessGame:
        with self._lock:
            key = str(channel_id)
            if key in self._games and not force:
                existing = self._games[key]
                raise ValueError(
                    f"A chess game is already active in this channel with "
                    f"{existing.player_name}. End it first."
                )
            if bot_color is None:
                bot_color = bool(random.getrandbits(1))
            game = ChessGame(
                game_id=uuid.uuid4().hex[:12],
                channel_id=key,
                player_id=str(player_id),
                player_name=player_name or "player",
                bot_color=bool(bot_color),
                started_at=datetime.now(timezone.utc).isoformat(),
                max_depth=max_depth,
                jitter=jitter,
            )
            self._games[key] = game
            self._save()
            return game

    def game_for(self, channel_id: str, player_id: str) -> ChessGame:
        """Return the active game, raising KeyError-ish ValueErrors."""
        game = self._games.get(str(channel_id))
        if game is None:
            raise ValueError(
                "No chess game is active in this channel. Start one with chess_start."
            )
        if str(player_id) != str(game.player_id):
            raise PermissionError(
                f"This chess game belongs to {game.player_name}; only they can play it."
            )
        return game

    def persist(self) -> None:
        with self._lock:
            self._save()

    def remove(self, channel_id: str) -> ChessGame | None:
        with self._lock:
            game = self._games.pop(str(channel_id), None)
            self._save()
            return game


# Singleton the tools share. Built lazily so the module import stays cheap.
_manager: ChessManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> ChessManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ChessManager()
    return _manager
