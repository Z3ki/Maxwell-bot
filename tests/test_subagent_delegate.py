"""The sub-agent delegation default: Maxwell's tool prompt steers heavy work
to sub_agent unless the runtime ``subagent_delegate`` control key is off.

This pins the behavioral half of "use sub-agents as the main way to do
actions". The execution half is covered by tests/test_subagent.py; here we
only pin that the *instruction* makes sub_agent the default executor on any
turn that carries it, and that the toggle actually flips it.
"""

from types import SimpleNamespace

from bot import MaxwellBot


class _FakeTool:
    def __init__(self, name):
        self.name = name

    def get_description(self):
        return f"<{self.name}>"


_CORE = {
    "send_message",
    "no_response",
    "react",
    "typing",
    "wait",
    "web_search",
    "fetch_url",
    "see_image",
    "see_video",
    "send_media",
    "send_meme",
    "image_generator",
    "more_tools",
    "usage",
}
_HEAVY = {
    "shell",
    "sub_agent",
    "create_site",
    "edit_site",
    "delete_site",
    "site_server",
    "list_sites",
}


def _prompt(subagent_delegate=True, include_subagent=True, content="build me a website"):
    names = set(_CORE) | set(_HEAVY)
    if not include_subagent:
        names.discard("sub_agent")
    fake = SimpleNamespace(
        tools={name: _FakeTool(name) for name in names},
        _control={
            "native_tool_calls": True,
            "tools_enabled": True,
            "subagent_delegate": subagent_delegate,
            "disabled_tools": [],
        },
    )
    msg = SimpleNamespace(
        _tools_expanded=False,
        content=content,
        attachments=[],
    )
    return MaxwellBot._tool_system_prompt.__get__(fake)(
        "discord", message=msg, content=content
    )


def test_delegation_block_is_rendered_when_sub_agent_present():
    prompt = _prompt()
    assert "sub_agent" in prompt
    assert "Delegate heavy work to sub_agent" in prompt


def test_toggle_off_drops_the_delegation_block():
    prompt = _prompt(subagent_delegate=False)
    assert "sub_agent" in prompt  # the tool is still there
    assert "Delegate heavy work to sub_agent" not in prompt


def test_no_sub_agent_means_no_delegation_block():
    prompt = _prompt(include_subagent=False)
    assert "sub_agent" not in prompt
    assert "Delegate heavy work to sub_agent" not in prompt


def test_plain_chat_turn_stays_lean():
    # "lol" is not an action request, so sub_agent leaves the turn's tool set
    # and the delegation block goes with it.
    prompt = _prompt(content="lol")
    assert "Delegate heavy work to sub_agent" not in prompt
