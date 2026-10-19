import asyncio
from types import SimpleNamespace

from plugins.github_projects.impl import GitHubProjectService


def test_proc_timeout_invokes_guest_cleanup(monkeypatch):
    cleaned = []
    killed = []

    class Proc:
        returncode = None

        async def communicate(self, input=None):
            await asyncio.sleep(10)

        def kill(self):
            killed.append(True)

        async def wait(self):
            return None

    async def create(*_args, **_kwargs):
        return Proc()

    async def cleanup():
        cleaned.append(True)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def run():
        try:
            await GitHubProjectService._proc(
                SimpleNamespace(),
                "docker",
                "exec",
                "x",
                timeout=0.01,
                on_timeout=cleanup,
            )
        except RuntimeError as exc:
            assert "timed out" in str(exc)
        else:
            raise AssertionError("timeout did not raise")

    asyncio.run(run())
    assert cleaned == [True]
    assert killed == [True]
