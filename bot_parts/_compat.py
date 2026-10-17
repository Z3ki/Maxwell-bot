"""Late-bound wrappers for helpers that tests patch on the ``bot`` module.

Methods split out of bot.py resolve module globals in their own file, so a
``monkeypatch.setattr(bot, "_get_shared_session", ...)`` would not reach them.
Bouncing the call through the real ``bot`` module keeps those patches working.
"""

import bot as _bot_module


async def _get_shared_session(*args, **kwargs):
    return await _bot_module._get_shared_session(*args, **kwargs)


async def _read_response_limited(*args, **kwargs):
    return await _bot_module._read_response_limited(*args, **kwargs)


async def record_reasoning(*args, **kwargs):
    return await _bot_module.record_reasoning(*args, **kwargs)
