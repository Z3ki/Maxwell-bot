import asyncio
from types import SimpleNamespace

import pytest

from plugins.maxwell_extras.task_jobs import Job, TaskJobs


class FakeTool:
    tool_name = "send_file"

    def __init__(self, sent):
        self.sent = sent

    async def execute(self, message, path=None, **kwargs):
        self.sent.append(path)
        return "__FILE_SENT__"


def _job():
    return Job(
        row={"id": "abcdefabcdef"},
        message=SimpleNamespace(),
        engine=None,
        reports=[{"status": "reported", "report": "Rendered /workspace/chart.png"}],
    )


@pytest.mark.parametrize(
    "gate_result, expected",
    [("refused: this tool is disabled", []), (None, ["/workspace/chart.png"])],
)
def test_reported_files_respect_tool_authorization(tmp_path, gate_result, expected):
    sent = []
    tool = FakeTool(sent)
    bot = SimpleNamespace(tools={"send_file": tool})
    bot._authorize_tool_execution = (
        lambda message, identifier, handler, arguments=None: gate_result
    )
    jobs = TaskJobs(bot, tmp_path / "jobs.sqlite3")
    asyncio.run(jobs._send_reported_files(_job()))
    assert sent == expected
