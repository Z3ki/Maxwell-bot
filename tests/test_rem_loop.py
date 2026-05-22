import asyncio

from memory import RemEventLog
from rem import RemStore, run_rem_once


class FakeMemory:
    def __init__(self):
        self.items = []

    def get_long_term_memory(self):
        return [{"id": i + 1, "content": v} for i, v in enumerate(self.items)]

    async def add_long_term_memory(self, content):
        self.items.append(content)
        return str(len(self.items))

    async def edit_long_term_memory(self, memory_id, content):
        self.items[int(memory_id) - 1] = content
        return True

    async def remove_long_term_memory(self, memory_id):
        del self.items[int(memory_id) - 1]
        return True


class FakeProvider:
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = 0

    async def generate_chat_completion(self, messages, tools=None, model=None, timeout=60):
        self.calls += 1
        return self.messages.pop(0)


def test_rem_loop_dispatches_tools_stops_done_and_records_run(tmp_path):
    async def run():
        log = RemEventLog(str(tmp_path), max_events=10)
        await log.record({"role": "user", "channel_id": "c", "user_id": "u", "user_name": "u", "content": "remember cats", "auto_mode": False})
        mem = FakeMemory()
        provider = FakeProvider([
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "ltm_add", "arguments": "{\"content\":\"User likes cats\"}"}}]},
            {"role": "assistant", "content": "DONE\n- added cats"},
        ])
        rem_run = await run_rem_once(memory_manager=mem, rem_log=log, provider=provider, data_dir=str(tmp_path), model="rem", max_turns=3)
        assert mem.items == ["User likes cats"]
        assert rem_run["tool_counts"] == {"ltm_add": 1}
        assert rem_run["audit"].startswith("DONE")
        assert len(await RemStore(str(tmp_path)).load_runs()) == 1
    asyncio.run(run())


def test_rem_loop_turn_cap_and_empty_slice_advances(tmp_path):
    async def run():
        log = RemEventLog(str(tmp_path), max_events=10)
        mem = FakeMemory()
        empty = await run_rem_once(memory_manager=mem, rem_log=log, provider=FakeProvider([]), data_dir=str(tmp_path), model="rem", max_turns=1)
        assert empty["events"] == 0
        state = await RemStore(str(tmp_path)).load_state()
        assert state["last_rem_run_ts"]

        await log.record({"role": "user", "channel_id": "c", "user_id": "u", "user_name": "u", "content": "new", "auto_mode": False})
        provider = FakeProvider([
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "ltm_list", "arguments": "{}"}}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "2", "function": {"name": "ltm_list", "arguments": "{}"}}]},
        ])
        rem_run = await run_rem_once(memory_manager=mem, rem_log=log, provider=provider, data_dir=str(tmp_path), model="rem", max_turns=1)
        assert rem_run["turns_used"] == 1
        assert "turn cap" in rem_run["audit"]
    asyncio.run(run())
