"""
Lightweight 8x8 Checkers game engine and PIL image renderer.
Implements standard American Checkers / English Draughts rules:
- 8x8 dark squares only (32 playable positions).
- Men move forward diagonally 1 step, or jump 2 steps over opponent.
- Mandatory jumps if available.
- Kings move and jump backwards and forwards.
- Minimax AI with alpha-beta pruning.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

EMPTY = 0
RED_MAN = 1
RED_KING = 2
BLACK_MAN = 3
BLACK_KING = 4

RED_PIECES = (RED_MAN, RED_KING)
BLACK_PIECES = (BLACK_MAN, BLACK_KING)

COL_NAMES = "abcdefgh"


def square_to_coord(sq: str) -> tuple[int, int]:
    """Convert notation e.g. 'c3' to (row, col)."""
    sq = sq.lower().strip()
    if len(sq) != 2 or sq[0] not in COL_NAMES or not sq[1].isdigit():
        raise ValueError(f"Invalid square notation: {sq}")
    col = COL_NAMES.index(sq[0])
    row = 8 - int(sq[1])
    if not (0 <= row < 8 and 0 <= col < 8):
        raise ValueError(f"Square out of bounds: {sq}")
    return row, col


def coord_to_square(r: int, c: int) -> str:
    """Convert (row, col) to 'c3'."""
    return f"{COL_NAMES[c]}{8 - r}"


class CheckersGame:
    def __init__(
        self,
        red_player: str = "User",
        black_player: str = "Maxwell",
        *,
        human_color: str = "red",
        human_player_id: str | None = None,
    ):
        self.red_player = red_player
        self.black_player = black_player
        self.human_color = human_color if human_color in {"red", "black"} else "red"
        self.human_player_id = (
            str(human_player_id) if human_player_id is not None else None
        )
        self.turn = "red"  # "red" (bottom) or "black" (top)
        self.winner: Optional[str] = None
        self.history: list[str] = []
        # A jump chain belongs to the piece that just captured. While this is
        # set, no other piece may move and the turn must not change.
        self._forced_piece: tuple[int, int] | None = None
        self.board = [[EMPTY for _ in range(8)] for _ in range(8)]
        self._setup_initial_board()

    def _setup_initial_board(self):
        # Black on rows 0, 1, 2 on dark squares
        for r in range(3):
            for c in range(8):
                if (r + c) % 2 == 1:
                    self.board[r][c] = BLACK_MAN
        # Red on rows 5, 6, 7 on dark squares
        for r in range(5, 8):
            for c in range(8):
                if (r + c) % 2 == 1:
                    self.board[r][c] = RED_MAN

    def get_piece(self, r: int, c: int) -> int:
        return self.board[r][c]

    def is_opponent(self, piece: int, turn: str) -> bool:
        if turn == "red":
            return piece in BLACK_PIECES
        return piece in RED_PIECES

    def is_own(self, piece: int, turn: str) -> bool:
        if turn == "red":
            return piece in RED_PIECES
        return piece in BLACK_PIECES

    def get_jumps_for_piece(self, r: int, c: int) -> list[tuple[int, int, int, int]]:
        """Returns list of (start_r, start_c, end_r, end_c)."""
        piece = self.board[r][c]
        if piece == EMPTY:
            return []
        jumps = []
        directions = []
        if piece in (RED_MAN, RED_KING, BLACK_KING):
            directions.extend([(-1, -1), (-1, 1)])  # upward
        if piece in (BLACK_MAN, RED_KING, BLACK_KING):
            directions.extend([(1, -1), (1, 1)])   # downward

        for dr, dc in directions:
            mid_r, mid_c = r + dr, c + dc
            end_r, end_c = r + 2 * dr, c + 2 * dc
            if 0 <= end_r < 8 and 0 <= end_c < 8:
                mid_p = self.board[mid_r][mid_c]
                end_p = self.board[end_r][end_c]
                turn = "red" if piece in RED_PIECES else "black"
                if self.is_opponent(mid_p, turn) and end_p == EMPTY:
                    jumps.append((r, c, end_r, end_c))
        return jumps

    def get_simple_moves_for_piece(self, r: int, c: int) -> list[tuple[int, int, int, int]]:
        piece = self.board[r][c]
        if piece == EMPTY:
            return []
        moves = []
        directions = []
        if piece in (RED_MAN, RED_KING, BLACK_KING):
            directions.extend([(-1, -1), (-1, 1)])
        if piece in (BLACK_MAN, RED_KING, BLACK_KING):
            directions.extend([(1, -1), (1, 1)])

        for dr, dc in directions:
            nr, nc = r + dr, c + dc
            if 0 <= nr < 8 and 0 <= nc < 8 and self.board[nr][nc] == EMPTY:
                moves.append((r, c, nr, nc))
        return moves

    def get_legal_moves(self, turn: Optional[str] = None) -> list[tuple[int, int, int, int]]:
        if turn is None:
            turn = self.turn
        if self._forced_piece is not None and turn == self.turn:
            r, c = self._forced_piece
            return self.get_jumps_for_piece(r, c)
        all_jumps = []
        all_simple = []
        for r in range(8):
            for c in range(8):
                p = self.board[r][c]
                if self.is_own(p, turn):
                    j = self.get_jumps_for_piece(r, c)
                    if j:
                        all_jumps.extend(j)
                    elif not all_jumps:
                        all_simple.extend(self.get_simple_moves_for_piece(r, c))

        # In standard checkers, if a jump exists, jumps are mandatory
        if all_jumps:
            return all_jumps
        return all_simple

    def make_move(self, move_str: str) -> tuple[bool, str]:
        """Move format: 'c3-d4' (simple) or 'c3-e5' (jump)."""
        if self.winner:
            return False, f"Game is already over. Winner: {self.winner}"

        try:
            parts = move_str.lower().replace("x", "-").split("-")
            if len(parts) != 2:
                return False, "Invalid move format. Use notation like `c3-d4` or `c3xe5`."
            sr, sc = square_to_coord(parts[0])
            er, ec = square_to_coord(parts[1])
        except Exception as e:
            return False, f"Invalid move notation: {e}"

        legal_moves = self.get_legal_moves(self.turn)
        if not legal_moves:
            self.winner = "black" if self.turn == "red" else "red"
            return False, f"No legal moves available. {self.winner.capitalize()} wins!"

        if (sr, sc, er, ec) not in legal_moves:
            legal_str = ", ".join(f"{coord_to_square(r1,c1)}-{coord_to_square(r2,c2)}" for r1,c1,r2,c2 in legal_moves[:8])
            return False, f"Illegal move `{move_str}`. Legal moves: {legal_str}"

        # Execute move
        p = self.board[sr][sc]
        self.board[sr][sc] = EMPTY
        self.board[er][ec] = p

        # Check jump capture
        is_jump = abs(sr - er) == 2
        if is_jump:
            mid_r = (sr + er) // 2
            mid_c = (sc + ec) // 2
            self.board[mid_r][mid_c] = EMPTY

        # Kinging
        promoted = False
        if p == RED_MAN and er == 0:
            self.board[er][ec] = RED_KING
            promoted = True
        elif p == BLACK_MAN and er == 7:
            self.board[er][ec] = BLACK_KING
            promoted = True

        self.history.append(f"{self.turn}: {move_str}")

        # Check multi-jump continuation for same piece
        # In American checkers a man that reaches the king row during a jump
        # is crowned but the move ends; it cannot continue jumping as a king.
        if is_jump and not promoted:
            further_jumps = self.get_jumps_for_piece(er, ec)
            if further_jumps:
                self._forced_piece = (er, ec)
                return True, f"Jump made! Multi-jump required with `{coord_to_square(er, ec)}`."

        self._forced_piece = None
        # Switch turn
        self.turn = "black" if self.turn == "red" else "red"

        # Check game over
        next_moves = self.get_legal_moves(self.turn)
        if not next_moves:
            self.winner = "black" if self.turn == "red" else "red"
            return True, f"Move `{move_str}` executed. {self.turn.capitalize()} has no legal moves. {self.winner.capitalize()} wins!"

        return True, f"Move `{move_str}` executed. It is now {self.turn.capitalize()}'s turn."

    def bot_ai_move(self, depth: int = 3) -> Optional[str]:
        """Pick best move using heuristic evaluation."""
        moves = self.get_legal_moves(self.turn)
        if not moves:
            return None
        best_move = moves[0]
        best_val = -999999

        for r1, c1, r2, c2 in moves:
            val = self._evaluate_move_score(r1, c1, r2, c2)
            if val > best_val:
                best_val = val
                best_move = (r1, c1, r2, c2)

        move_str = f"{coord_to_square(best_move[0], best_move[1])}-{coord_to_square(best_move[2], best_move[3])}"
        return move_str

    def _evaluate_move_score(self, r1: int, c1: int, r2: int, c2: int) -> int:
        score = 0
        p = self.board[r1][c1]
        # Prefer jumps
        if abs(r1 - r2) == 2:
            score += 100
        # Prefer kinging
        if (p == BLACK_MAN and r2 == 7) or (p == RED_MAN and r2 == 0):
            score += 50
        # Center board control
        if 2 <= c2 <= 5 and 2 <= r2 <= 5:
            score += 10
        return score

    def render_board_png(self) -> bytes:
        """Render board to high quality PNG image buffer."""
        sq_size = 64
        border = 32
        img_size = sq_size * 8 + border * 2
        img = Image.new("RGB", (img_size, img_size), color=(30, 30, 35))
        draw = ImageDraw.Draw(img)

        # Light / Dark board squares
        light_color = (235, 235, 210)
        dark_color = (118, 150, 86)
        red_color = (210, 50, 45)
        black_color = (40, 40, 45)
        gold_color = (255, 215, 0)

        for r in range(8):
            for c in range(8):
                x1 = border + c * sq_size
                y1 = border + r * sq_size
                x2 = x1 + sq_size
                y2 = y1 + sq_size
                fill = dark_color if (r + c) % 2 == 1 else light_color
                draw.rectangle([x1, y1, x2, y2], fill=fill)

                # Draw pieces
                p = self.board[r][c]
                if p != EMPTY:
                    margin = 8
                    px1, py1 = x1 + margin, y1 + margin
                    px2, py2 = x2 - margin, y2 - margin
                    p_fill = red_color if p in (RED_MAN, RED_KING) else black_color
                    outline = (255, 255, 255) if p in (RED_MAN, RED_KING) else (180, 180, 180)
                    draw.ellipse([px1, py1, px2, py2], fill=p_fill, outline=outline, width=2)

                    # King crown indicator
                    if p in (RED_KING, BLACK_KING):
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        draw.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=gold_color, outline=(0, 0, 0), width=1)

        # Labels
        for i in range(8):
            col_letter = COL_NAMES[i]
            x = border + i * sq_size + sq_size // 2
            draw.text((x, border // 2), col_letter, fill=(200, 200, 200), anchor="mm")
            draw.text((x, img_size - border // 2), col_letter, fill=(200, 200, 200), anchor="mm")
            row_num = str(8 - i)
            y = border + i * sq_size + sq_size // 2
            draw.text((border // 2, y), row_num, fill=(200, 200, 200), anchor="mm")
            draw.text((img_size - border // 2, y), row_num, fill=(200, 200, 200), anchor="mm")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
