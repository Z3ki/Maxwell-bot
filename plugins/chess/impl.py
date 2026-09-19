"""Tool implementations for the chess plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

class ChessStartTool(Tool):
    """Start a new chess game in this channel against a chosen player."""
    tool_name = 'chess_start'
    returns_result = True
    ends_turn = False


    def get_description(self):
        name = _chess_bot_name(self.bot)
        return (
            f"Start a chess game in this channel. Pass opponent= to choose who "
            f"{name} plays — a mention, Discord user id, or display name. Omit "
            f"it to play the person who asked, or the single @mentioned human "
            f"in the message. One game per channel; only that opponent may "
            f"move. Posts the starting board image and returns FEN plus the "
            f"annotated legal moves. {name} plays his own moves: if {name} is "
            f"white, this returns the opening position and you pick the first "
            f"move with chess_move. Params: opponent, bot_side (white|black|"
            f"auto, default white)."
        )

    async def execute(
        self,
        message: Message,
        bot_side: str | None = "white",
        depth: int | None = None,
        opponent: str | None = None,
        player: str | None = None,
        **kwargs,
    ) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        name = _chess_bot_name(self.bot)
        if not channel_id:
            return "Error: no channel context."
        try:
            author_id, author_name = _chess_resolve_player(
                message, opponent or player, self.bot
            )
        except ValueError as exc:
            return f"Error: {exc}"

        manager = _chess_get_manager()
        existing = manager.active(channel_id)
        if existing is not None:
            return (
                f"Error: a chess game is already active in this channel. "
                f"It is between {name} and {existing.player_name}. "
                f"Use chess_state to see it, or chess_resign to end it first."
            )

        side = str(bot_side or "auto").strip().lower()
        if side in ("auto", "random"):
            bot_color = None
        elif side == "white":
            bot_color = _chess.WHITE if hasattr(_chess, "WHITE") else True
        elif side == "black":
            bot_color = _chess.BLACK if hasattr(_chess, "BLACK") else False
        else:
            return "Error: bot_side must be 'white', 'black', or 'auto'."

        # depth/jitter only ever reach the local search, which is now just the
        # wedge-breaker for when Maxwell repeatedly fails to name a legal move.
        # Kept accepted-but-clamped so an old caller passing depth= is not an
        # error, and stored on the game so the fallback still has settings.
        max_depth = int(depth or 3)
        if max_depth < 1:
            max_depth = 1
        if max_depth > 4:
            max_depth = 4

        game = manager.start(
            channel_id,
            author_id,
            author_name,
            bot_color=bot_color,
            max_depth=max_depth,
            jitter=0.35,
        )

        # Maxwell plays his own chess. If he has the white side he does NOT get
        # an engine move dropped in here — the tool returns the annotated
        # position and he names his own opening move on the follow-up turn
        # (chess_start is in RESULT_TOOL_NAMES, so that turn always happens).
        _chess_clear_misses(game.game_id)

        self._signal_streaming(message)
        cdn_url, local_path, png = await _chess_post_board(self.bot, message, game)
        manager.persist()
        await _chess_record(
            self.bot,
            message,
            f"started a chess game. {name} is "
            f"{_chess_color_name(game.bot_color)}, "
            f"{game.player_name} is {_chess_color_name(game.player_color)}.",
        )

        result = _chess_game_result(
            game,
            posted=True,
            cdn_url=cdn_url,
            local_path=local_path,
            png=png,
            bot_name=name,
        )
        if game.bot_turn and not game.is_over:
            result += (
                "\n\nGame started and it is "
                + name
                + "'s move — YOUR move. Pick from the legal moves above and "
                "call chess_move(move='<SAN>') now. Say your move in chat too."
            )
        else:
            result += (
                "\n\nGame started. It is "
                + game.player_name
                + "'s move — prompt them to play."
            )
        result += " Use chess_move to advance the game."
        return result

class ChessMoveTool(Tool):
    """Play a chess move: the player's move or this bot's own move."""
    tool_name = 'chess_move'
    returns_result = True
    ends_turn = False


    def get_description(self):
        name = _chess_bot_name(self.bot)
        return (
            "Advance the chess game by one move. Pass move= in SAN (e4, Nf3, "
            "O-O, exd5, Qh5) or UCI (e2e4, e7e8q). When it is the player's "
            f"turn, relay THEIR move. When it is {name}'s turn, YOU choose "
            f"{name}'s move — there is no engine playing for you. The result "
            "lists every legal move annotated with what it captures, whether "
            "it checks or mates, and whether the piece would just be taken; "
            "read it and pick the best one. Posts the updated board image and "
            "returns FEN + the new position. Only the chosen opponent may "
            "call it with their move."
        )

    async def execute(
        self,
        message: Message,
        move: str | None = None,
        respond: bool = True,
        **kwargs,
    ) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        author_id = str(getattr(message.author, "id", "") or "")
        manager = _chess_get_manager()
        try:
            game = manager.game_for(channel_id, author_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except PermissionError as exc:
            return f"Error: {exc}"

        played: list[str] = []
        error_text: str | None = None
        engine_fallback = False
        try:
            if game.bot_turn:
                # Maxwell's own turn. He names the move; the local search is
                # only reached after repeated failures to name a legal one, so
                # a game can never wedge on his turn.
                if move:
                    mv = game.parse_move(move)
                    played.append(game.apply_move(mv))
                    _chess_clear_misses(game.game_id)
                elif _chess_miss_count(game.game_id) >= _CHESS_MAX_MISSES:
                    mv, san = _chess_choose_bot_move(
                        game.board, depth=game.max_depth, jitter=game.jitter
                    )
                    game.apply_move(mv)
                    played.append(san)
                    engine_fallback = True
                    _chess_clear_misses(game.game_id)
                    logger.warning(
                        "chess: falling back to local search in %s after %d "
                        "failed attempts to name a legal move",
                        channel_id,
                        _CHESS_MAX_MISSES,
                    )
                else:
                    misses = _chess_note_miss(game.game_id)
                    manager.persist()
                    state = _chess_state_text(game, bot_name=_chess_bot_name(self.bot))
                    return (
                        "Error: it is your move and you did not name one. "
                        "Choose from the legal moves below and call "
                        "chess_move(move='<SAN>') again "
                        f"(attempt {misses} of {_CHESS_MAX_MISSES}).\n\n" + state
                    )
            else:
                # Player to move. They must supply a move; it must be legal for
                # the side to move (parse_move enforces that).
                if not move:
                    return (
                        f"Error: it is {game.player_name}'s move. "
                        "Pass move= (e.g. move=...'e4') with the move they just "
                        "played. Leave move unset and it stays on the player."
                    )
                mv = game.parse_move(move)
                played.append(game.apply_move(mv))
        except ValueError as exc:
            error_text = str(exc)
        except Exception as exc:
            error_text = f"could not apply move: {exc}"

        if error_text:
            if game.bot_turn:
                # An illegal move from Maxwell himself: hand back the position
                # so the next attempt is informed, and count it toward the
                # fallback so a stubborn loop still ends in a played move.
                misses = _chess_note_miss(game.game_id)
                state = _chess_state_text(game, bot_name=_chess_bot_name(self.bot))
                return (
                    f"Error: {error_text}\nThat move is not legal here "
                    f"(attempt {misses} of {_CHESS_MAX_MISSES}). Pick one of "
                    "these exactly as written.\n\n" + state
                )
            return f"Error: {error_text}"

        # The human just moved and it is now Maxwell's turn. Do not pick his
        # move here — chess_move returns its result to the model, so he plays
        # it himself on the follow-up turn with the annotated position in hand.
        bot_to_move = respond and game.bot_turn and not game.is_over

        manager.persist()
        self._signal_streaming(message)
        cdn_url, local_path, png = await _chess_post_board(self.bot, message, game)
        manager.persist()
        await _chess_record(
            self.bot,
            message,
            "chess move(s): " + " ".join(played) + f" · fen {game.fen.split()[0]}",
        )

        name = _chess_bot_name(self.bot)
        result = _chess_game_result(
            game,
            posted=True,
            cdn_url=cdn_url,
            local_path=local_path,
            png=png,
            bot_name=name,
        )
        tail = "\n\nPlayed move(s): " + " ".join(played)
        if engine_fallback:
            tail += (
                f"\n(You did not name a legal move {_CHESS_MAX_MISSES} times, "
                "so the local search played one for you. Pick your own next time.)"
            )
        if game.is_over:
            tail += "\nThe game is over."
        elif bot_to_move:
            tail += (
                f"\nIt is {name}'s move NOW — your move. Pick from the legal "
                "moves above and call chess_move(move='<SAN>') in this same "
                "batch. Do not tell the player it is their turn."
            )
        elif not game.bot_turn:
            tail += "\nIt is now the player's move — tell them it is their turn."
        else:
            tail += (
                f"\nIt is {name}'s move — pick from the legal moves above and "
                "call chess_move(move='<SAN>')."
            )
        return result + tail

class ChessStateTool(Tool):
    """Show the current chess board, FEN, and legal moves (no board change)."""
    tool_name = 'chess_state'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Get the current chess board state (text board, FEN, legal moves, "
            "whose move it is) for the active game in this channel. Does NOT "
            "change the board or post an image; use it to re-sync when you lose "
            "track of the position. Only the chosen opponent may call it."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        author_id = str(getattr(message.author, "id", "") or "")
        manager = _chess_get_manager()
        try:
            game = manager.game_for(channel_id, author_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except PermissionError as exc:
            return f"Error: {exc}"
        png = _chess_render_safe(game)
        return _chess_game_result(
            game, posted=False, png=png, bot_name=_chess_bot_name(self.bot)
        )

class ChessResignTool(Tool):
    """End the current chess game."""
    tool_name = 'chess_resign'
    returns_result = True
    ends_turn = False


    def get_description(self):
        name = _chess_bot_name(self.bot)
        return (
            "End the active chess game in this channel. Pass "
            f"side={name.lower()} to have {name} resign, side=player to record "
            "the player resigning, or leave it default for a mutual end. "
            "Returns the final board and result. Only the chosen opponent may "
            "call it."
        )

    async def execute(
        self,
        message: Message,
        side: str | None = None,
        **kwargs,
    ) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        author_id = str(getattr(message.author, "id", "") or "")
        manager = _chess_get_manager()
        try:
            game = manager.game_for(channel_id, author_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except PermissionError as exc:
            return f"Error: {exc}"

        who = str(side or "player").strip().lower()
        if who in ("player", "user", "human"):
            winner = _chess_color_name(game.bot_color)
        elif _chess_is_bot_resign(who, self.bot):
            winner = _chess_color_name(game.player_color)
        else:
            winner = None
        manager.remove(channel_id)
        final = _chess_state_text(game, bot_name=_chess_bot_name(self.bot))
        png = _chess_render_safe(game)
        if winner:
            final += f"\nGAME OVER: {winner} wins by resignation."
        else:
            final += "\nGAME OVER: the game was ended."
        final = _chess_append_image(final, png)
        await _chess_record(self.bot, message, f"chess game ended ({who} resigned).")
        return final + "\n\nGame ended. Use chess_start to begin a new one."
