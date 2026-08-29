"""
Checkers game tools for Maxwell.
Registers checkers_start, checkers_move, checkers_state, checkers_resign.
"""
from __future__ import annotations

import io
import logging
import os
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


class CheckersStartTool(Tool):
    def __init__(self, bot=None):
        self.bot = bot

    def get_name(self) -> str:
        return "checkers_start"

    def get_description(self) -> str:
        return (
            "Start a new Checkers (draughts) match in this channel against Maxwell. "
            "Red moves first (at the bottom). Maxwell plays Black."
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
        channel_id = str(message.channel.id)
        user_name = getattr(message.author, "display_name", "User")
        game = CheckersGame(red_player=user_name, black_player="Maxwell")
        set_game(channel_id, game)

        png_bytes = game.render_board_png()
        file = discord.File(io.BytesIO(png_bytes), filename="checkers_board.png")
        await message.channel.send(
            f"🔴 **Checkers match started!** {user_name} (Red) vs Maxwell (Black).\n"
            f"Red moves first. Use `checkers_move(move='c3-d4')` or notation like `c3-d4`.",
            file=file,
        )
        legal_moves = game.get_legal_moves("red")
        legal_str = ", ".join(
            f"{game.history[-1] if False else ''}" or f"{m[0]},{m[1]}->{m[2]},{m[3]}"
            for m in legal_moves[:6]
        )
        return f"Checkers game started. Legal moves for Red: {len(legal_moves)} options available."


class CheckersMoveTool(Tool):
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

        ok, msg = game.make_move(move)
        if not ok:
            return f"Move failed: {msg}"

        # If it was user move and game is not over and it's Maxwell's turn (black)
        ai_msg = ""
        if not game.winner and game.turn == "black":
            bot_move = game.bot_ai_move()
            if bot_move:
                bot_ok, bot_m = game.make_move(bot_move)
                if bot_ok:
                    ai_msg = f"\nMaxwell played `{bot_move}`."

        png_bytes = game.render_board_png()
        file = discord.File(io.BytesIO(png_bytes), filename="checkers_board.png")
        status_text = f"**Checkers**: {msg}{ai_msg}"
        if game.winner:
            status_text += f"\n🏆 **{game.winner.capitalize()} wins the match!**"
            remove_game(channel_id)

        await message.channel.send(status_text, file=file)
        return f"Move executed. {msg}{ai_msg}"


class CheckersStateTool(Tool):
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

        user_name = getattr(message.author, "display_name", "Player")
        remove_game(channel_id)
        await message.channel.send(f"🏳️ {user_name} has resigned the Checkers match. Game over.")
        return f"{user_name} resigned the game."
