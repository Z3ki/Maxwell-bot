"""Base Tool class for Maxwell Bot"""

from abc import ABC, abstractmethod
from typing import Any

from discord import Message


class Tool(ABC):
    """Base class for bot tools"""

    # Tools flagged destructive require user confirmation when the current
    # message context is "tainted" (e.g. just received content from
    # fetch_url / web_search). This is the second line of defense against
    # indirect prompt injection: even if a malicious page tricks the model
    # into proposing a shell command, the user has to click Confirm before
    # it runs. Default off for harmless read tools.
    is_destructive: bool = False

    def __init__(self, bot):
        self.bot = bot
        self.name = self.__class__.__name__

    @abstractmethod
    def get_description(self) -> str:
        pass

    @abstractmethod
    async def execute(self, message: Message, **kwargs) -> Any:
        pass

    def _signal_streaming(self, message: Any = None) -> None:
        """Notify the live progress message that this tool is about to post
        its own output (shell stdout, an image, a poll, an attachment).

        Tools that call message.channel.send() mid-execution should call this
        right before the first send. The progress message in the channel
        (managed by tool_progress.ToolProgress) will then delete itself so
        the user doesn't see two parallel "working on it" + tool output
        streams.

        No-op when:
          - Progress messages are off for this server (no progress object)
          - A sibling tool in the same batch is still running and the
            progress message is shared — the deletion only happens if this
            is the sole non-terminal tool in the batch. Either way the
            progress message is cleaned up at stop(), so a missed signal
            is just a few hundred ms of overlap, not a stuck message.
          - Tests / standalone use (no bot attached)

        Per-channel lookup. The old single-attribute lookup stomped
        across channels under load: tool B in channel Y would call
        notify_streaming on the progress for channel X's tool batch,
        which would then prematurely delete the wrong message. The
        tool now resolves the channel id from the message it received
        (passed in via ``message``) and looks up that channel's
        progress in the bot's per-channel dict. Falls back to "any
        progress in the dict" if no message is passed (back-compat
        for the existing call sites that don't have ``message`` in
        scope — those are usually inner helpers called from one place,
        so the wrong-channel risk is minimal).

        The base class helper is preferred over a free function so tools
        can grep for it: `grep -n _signal_streaming bot_tools.py` shows
        every tool that streams its own output.
        """
        bot = getattr(self, "bot", None)
        if bot is None:
            return
        per_chan = getattr(bot, "_current_progress_by_channel", None)
        if per_chan is None:
            return
        if message is not None:
            chan_id = str(getattr(getattr(message, "channel", None), "id", ""))
            if chan_id:
                progress = per_chan.get(chan_id)
                if progress is not None:
                    progress.notify_streaming()
                return
        # No message was passed (or message had no channel id). Pick the
        # only entry if the dict has exactly one — this is the
        # back-compat path for tools whose inner helper doesn't have
        # ``message`` in scope. Under real load with many channels,
        # callers MUST pass ``message`` to avoid cross-channel
        # contamination.
        if len(per_chan) == 1:
            progress = next(iter(per_chan.values()))
            if progress is not None:
                progress.notify_streaming()
