"""Offline regressions for image reuse, speech decoding and finished games."""

import asyncio
import base64
import json
import re
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import chess
import pytest
from PIL import Image

from chess_game import ChessGame
from generated_artifacts import begin_generated_files, reset_generated_files
from plugins.chess import impl as chess_tools
from plugins.images import impl as image_tools
from plugins.tts_voice.impl import SpeechError, synthesize_speech


class SpeechResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return self.body


@pytest.mark.parametrize("header", [b"\xff\xf3", b"\xff\xf2", b"\xff\xe3", b"\xff\xfa"])
def test_speech_accepts_mp3_frame_headers_without_id3(monkeypatch, header):
    audio = header + b"\x80\x00audio-fixture"
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: SpeechResponse(audio))
    assert synthesize_speech("hello", "voice", "test-key", "model") == audio


@pytest.mark.parametrize("body", [b"\x80invalid", b"[]", b"null"])
def test_invalid_speech_response_reports_provider_error(monkeypatch, body):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: SpeechResponse(body))
    with pytest.raises(SpeechError, match="Mistral TTS returned"):
        synthesize_speech("hello", "voice", "test-key", "model")


def test_generated_image_can_be_edited_without_hosting(tmp_path, monkeypatch):
    async def scenario():
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
        image_bytes = buffer.getvalue()
        bot = SimpleNamespace(config=SimpleNamespace(
            AI_BASE_URL="https://image.example.invalid/v1",
            MAXWELL_PUBLIC_BASE_URL="", MAXWELL_SITE_DIR=str(tmp_path),
        ))
        posted = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def text(self):
                data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
                return json.dumps({"choices": [{"message": {"content": data_uri}}]})

        def post(url, **kwargs):
            posted.append(kwargs["json"])
            return Response()

        async def session():
            return SimpleNamespace(post=post)

        monkeypatch.setattr(image_tools, "_get_shared_session", session)
        token = begin_generated_files()
        try:
            generated = image_tools._generated_image_result(bot, image_bytes, prefix="image", summary="generated")
            match = re.search(r"Attachment path: (\S+)", generated)
            assert match is not None, generated
            path = match.group(1)
            tool = image_tools.HDImageGeneratorTool(bot)
            edited = await tool.execute(SimpleNamespace(attachments=[]), prompt="make it blue", image=path)
            assert "successfully" in edited, edited
            assert len(posted) == 1
            parts = posted[0]["messages"][0]["content"]
            assert [part["type"] for part in parts] == ["text", "image_url"]
            assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
            other = begin_generated_files()
            try:
                raw, error = await tool._load_one(path)
                assert raw is None and "unavailable" in error
            finally:
                reset_generated_files(other)
        finally:
            reset_generated_files(token)
        raw, error = await tool._load_one(path)
        assert raw is None and "unavailable" in error

    asyncio.run(scenario())


def test_local_image_path_is_requester_scoped(tmp_path, monkeypatch):
    async def scenario():
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(buffer, format="PNG")
        image_bytes = buffer.getvalue()
        bot = SimpleNamespace(config=SimpleNamespace(
            AI_BASE_URL="https://image.example.invalid/v1",
            MAXWELL_PUBLIC_BASE_URL="",
            MAXWELL_SITE_DIR=str(tmp_path),
        ))
        generated = image_tools._generated_image_result(
            bot, image_bytes, prefix="image", summary="generated", requester_id="111",
        )
        match = re.search(r"Local path: (\S+)", generated)
        assert match is not None, generated
        path = match.group(1)
        tool = image_tools.HDImageGeneratorTool(bot)

        owned, error = await tool._load_one(path, "111")
        assert error == "" and owned == image_bytes
        stolen, error = await tool._load_one(path, "222")
        assert stolen is None and "not owned" in error
        anonymous, error = await tool._load_one(path)
        assert anonymous is None and "not owned" in error

        planted = Path(tmp_path) / "_images" / "planted.png"
        planted.write_bytes(image_bytes)
        raw, error = await tool._load_one(str(planted), "111")
        assert raw is None and "not owned" in error

        outside = tmp_path / "secret.png"
        outside.write_bytes(b"secret-bytes")
        raw, error = await tool._load_one(str(outside), "111")
        assert raw is None and "outside" in error
        link = Path(tmp_path) / "_images" / "linked.png"
        link.symlink_to(outside)
        raw, error = await tool._load_one(str(link), "111")
        assert raw is None
        assert raw != b"secret-bytes"

        monkeypatch.chdir(tmp_path)
        scratch = Path("temp")
        scratch.mkdir()
        (scratch / "other-request.png").write_bytes(b"other-request")
        raw, error = await tool._load_one("temp/other-request.png", "111")
        assert raw is None and "outside" in error

        posted = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def text(self):
                return "{}"

        def post(url, **kwargs):
            posted.append(kwargs)
            return Response()

        async def session():
            return SimpleNamespace(post=post)

        monkeypatch.setattr(image_tools, "_get_shared_session", session)
        rejected = await tool.execute(
            SimpleNamespace(author=SimpleNamespace(id="222"), attachments=[]),
            prompt="reuse the other user's image",
            image=path,
        )
        assert "not owned" in rejected
        assert posted == []

    asyncio.run(scenario())


def drawn_game():
    game = ChessGame(game_id="fixture", channel_id="1", player_id="2", player_name="Player",
                     bot_color=chess.BLACK, started_at="2026-11-09T00:00:00+00:00")
    game.board = chess.Board("7k/8/8/8/8/8/P7/KR6 w - - 100 51")
    assert game.is_over
    return game


def test_finished_draw_cannot_be_reopened_by_pawn_move():
    game = drawn_game()
    before = game.fen
    move = game.parse_move("a3")
    with pytest.raises(ValueError, match="already over"):
        game.apply_move(move)
    assert game.fen == before
    assert game.history == []


def test_chess_tool_rejects_move_after_draw_without_posting(monkeypatch):
    game = drawn_game()
    monkeypatch.setattr(chess_tools, "_chess_get_manager", lambda: SimpleNamespace(game_for=lambda *_: game))
    message = SimpleNamespace(channel=SimpleNamespace(id=1), author=SimpleNamespace(id=2))
    before = game.fen
    result = asyncio.run(chess_tools.ChessMoveTool(SimpleNamespace()).execute(message, move="a3"))
    assert result.startswith("Error:") and "already over" in result
    assert game.fen == before


@pytest.mark.parametrize("size", [5, 6, 7])
def test_inline_image_cannot_bypass_input_size_limit(size):
    tool = image_tools.HDImageGeneratorTool(SimpleNamespace())
    tool.MAX_INPUT_BYTES = 4
    ref = "data:image/png;base64," + base64.b64encode(b"x" * size).decode()
    raw, error = asyncio.run(tool._load_one(ref))
    assert raw is None
    assert "input size limit" in error


def test_inline_image_accepts_exact_input_size_limit():
    tool = image_tools.HDImageGeneratorTool(SimpleNamespace())
    tool.MAX_INPUT_BYTES = 4
    ref = "data:image/png;base64," + base64.b64encode(b"1234").decode()
    assert asyncio.run(tool._load_one(ref)) == (b"1234", "")
