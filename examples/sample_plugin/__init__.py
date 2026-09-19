"""Minimal sample plugin. Copy this directory into plugins/ to install it."""

from __future__ import annotations

from typing import Any

from tools import Tool


class SampleEchoTool(Tool):
    tool_name = "sample_echo"
    returns_result = True
    side_effects = False

    def get_description(self) -> str:
        return "Echo text back to the model. Params: text (required)."

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        }

    async def execute(self, message: Any, text: str = "", **kwargs: Any) -> str:
        return text or "sample_echo is ready."


def setup(bot, ctx):
    return [SampleEchoTool(bot)]
