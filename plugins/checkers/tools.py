"""
Checkers game tools for Maxwell.
Registers checkers_start, checkers_move, checkers_state, checkers_resign.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Optional

import discord

from tools import Tool
from .checkers_game import CheckersGame

logger = logging.getLogger(__name__)

# In-memory games keyed by channel_id (str)
_ACTIVE_GAMES: dict[str, CheckersGame] = {}


def get_game(channel_id: str) -> Optional[CheckersGame]:
    return _ACTIVE_GAMES.get(str(channel_id))


def set_game(channel_id: str, game: CheckersGame):
    _ACTIVE_GAMES[str(channel_id)] = game


def remove_game(channel_id: str):
    _ACTIVE_GAMES.pop(str(channel_id), None)


def _run_maxwell_turn(game: CheckersGame) -> list[str]:
    """Finish Maxwell's turn, including every forced jump in a chain."""
    moves = []
    for _ in range(32):  # a checkers position cannot need more than 32 captures
        if game.winner or game.turn == game.human_color:
            break
        move = game.bot_ai_move()
        if not move:
            break
        ok, _result = game.make_move(move)
        if not ok:
            break
        moves.append(move)
    return moves


class CheckersStartTool(Tool):
    returns_result = True

    def __init__(self, bot=None):
        self.bot = bot

    def get_name(self) -> str:
        return "checkers_start"

    def get_description(self) -> str:
        return (
            "Start a new Checkers (draughts) match in this channel against Maxwell. "
            "Red moves first; choose user_color red or black."
        )

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_color": {
                    "type": "string",
                    "enum": ["red", "black"],
                    "description": "Color for the user (default: red)",
                    "default": "red",
                }
            },
        }

    async def execute(self, message: Any, user_color: str = "red", **kwargs) -> str:
        user_color = str(user_color or "red").strip().lower()
        if user_color not in {"red", "black"}:
            return "Error: user_color must be `red` or `black`."
        channel_id = str(message.channel.id)
        user_name = getattr(message.author, "display_name", "User")
        user_id = getattr(message.author, "id", None)
        game = CheckersGame(
            red_player=user_name if user_color == "red" else "Maxwell",
            black_player="Maxwell" if user_color == "red" else user_name,
            human_color=user_color,
            human_player_id=user_id,
        )
        opening_moves = _run_maxwell_turn(game)
        opening = (
            "Maxwell played " + " ".join(f"`{move}`" for move in opening_moves) + "."
            if opening_moves
            else ""
        )
        png_bytes = game.render_board_png()
        file = discord.File(io.BytesIO(png_bytes), filename="checkers_board.png")
        await message.channel.send(
            f"🔴 **Checkers match started!** {game.red_player} (Red) vs "
            f"{game.black_player} (Black).\n"
            f"{opening + ' ' if opening else ''}"
            f"It is {game.turn.capitalize()}'s turn. Use "
            "`checkers_move(move='c3-d4')` or notation like `c3-d4`.",
            file=file,
        )
        set_game(channel_id, game)
        legal_moves = game.get_legal_moves("red")
        return f"Checkers game started. Legal moves for Red: {len(legal_moves)} options available."


class CheckersMoveTool(Tool):
    returns_result = True

    def __init__(self, bot=None):
        self.bot = bot

    def get_name(self) -> str:
        return "checkers_move"

    def get_description(self) -> str:
        return (
            "Play a move in the active Checkers match in this channel. "
            "Notation: 'c3-d4' for simple move, 'c3-e5' for jump."
        )

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "move": {
                    "type": "string",
                    "description": "Move string in notation like 'c3-d4' or 'c3-e5'",
                }
            },
            "required": ["move"],
        }

    async def execute(self, message: Any, move: str, **kwargs) -> str:
        channel_id = str(message.channel.id)
        game = get_game(channel_id)
        if not game:
            return "Error: No active Checkers game in this channel. Start one with `checkers_start`."
        author_id = getattr(getattr(message, "author", None), "id", None)
        if (
            game.human_player_id is not None
            and author_id is not None
            and str(author_id) != game.human_player_id
        ):
            return "Error: only the player who started this match can move."
        if game.turn != game.human_color:
            return f"Error: it is {game.turn}'s turn; Maxwell is thinking."

        ok, msg = game.make_move(move)
        if not ok:
            return f"Move failed: {msg}"

        # Let Maxwell finish every forced jump in one turn. American checkers
        # requires a capture chain to stay with the same piece, so handing
        # control back after only the first AI jump can strand the match on
        # Maxwell's turn forever.
        ai_moves = _run_maxwell_turn(game)
        ai_msg = ""
        if ai_moves:
            ai_msg = "\n" + " ".join(f"Maxwell played `{item}`." for item in ai_moves)

        png_bytes = game.render_board_png()
        file = discord.File(io.BytesIO(png_bytes), filename="checkers_board.png")
        status_text = f"**Checkers**: {msg}{ai_msg}"
        if game.winner:
            status_text += f"\n🏆 **{game.winner.capitalize()} wins the match!**"
            remove_game(channel_id)

        await message.channel.send(status_text, file=file)
        return f"Move executed. {msg}{ai_msg}"


class CheckersStateTool(Tool):
    returns_result = True

    def __init__(self, bot=None):
        self.bot = bot

    def get_name(self) -> str:
        return "checkers_state"

    def get_description(self) -> str:
        return "Inspect the board state and whose turn it is in the active Checkers match."

    def get_parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, message: Any, **kwargs) -> str:
        channel_id = str(message.channel.id)
        game = get_game(channel_id)
        if not game:
            return "No active Checkers game in this channel."

        png_bytes = game.render_board_png()
        file = discord.File(io.BytesIO(png_bytes), filename="checkers_board.png")
        await message.channel.send(
            f"**Checkers Game State**:\n"
            f"• Red: {game.red_player}\n"
            f"• Black: {game.black_player}\n"
            f"• Active Turn: **{game.turn.capitalize()}**\n"
            f"• Moves Played: {len(game.history)}",
            file=file,
        )
        return f"Checkers active: {game.turn.capitalize()}'s turn."


class CheckersResignTool(Tool):
    returns_result = True

    def __init__(self, bot=None):
        self.bot = bot

    def get_name(self) -> str:
        return "checkers_resign"

    def get_description(self) -> str:
        return "Forfeit/resign the active Checkers game in this channel."

    def get_parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, message: Any, **kwargs) -> str:
        channel_id = str(message.channel.id)
        game = get_game(channel_id)
        if not game:
            return "No active Checkers game to resign."

        author_id = getattr(getattr(message, "author", None), "id", None)
        if (
            game.human_player_id is not None
            and author_id is not None
            and str(author_id) != game.human_player_id
        ):
            return "Error: only the player who started this match can resign."
        user_name = getattr(message.author, "display_name", "Player")
        remove_game(channel_id)
        await message.channel.send(f"🏳️ {user_name} has resigned the Checkers match. Game over.")
        return f"{user_name} resigned the game."
