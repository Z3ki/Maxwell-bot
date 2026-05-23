import asyncio
from types import SimpleNamespace

from bot import MaxwellBot


class FakeTool:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def execute(self, message, **params):
        self.calls.append(params)
        return self.result


def test_process_tool_calls_preserves_no_response_marker_for_tts():
    tts = FakeTool("__NO_RESPONSE__")
    bot = SimpleNamespace(
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"tts": tts},
    )
    message = SimpleNamespace()

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '{"tool":"tts","text":"say this"}',
        )
        assert response == ""
        assert tool_results == ["Tool tts: __NO_RESPONSE__"]
        assert tts.calls == [{"text": "say this"}]

    asyncio.run(run())


def test_process_tool_calls_still_returns_other_tool_results():
    react = FakeTool("Reacted with <:catjam:123>")
    bot = SimpleNamespace(
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"react": react},
    )
    message = SimpleNamespace()

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '{"tool":"react","emoji":"catjam"}',
        )
        assert response == ""
        assert tool_results == ["Tool react: Reacted with <:catjam:123>"]
        assert react.calls == [{"emoji": "catjam"}]

    asyncio.run(run())


def test_process_tool_calls_strips_disabled_tool_call():
    react = FakeTool("Reacted with <:catjam:123>")
    bot = SimpleNamespace(
        _control={"tools_enabled": True, "disabled_tools": ["react"], "typing_indicator": False},
        tools={"react": react},
    )
    message = SimpleNamespace()

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '{"tool":"react","emoji":"catjam"}',
        )
        assert response == ""
        assert tool_results == ["Tool react: Error - tool is disabled"]
        assert react.calls == []

    asyncio.run(run())
