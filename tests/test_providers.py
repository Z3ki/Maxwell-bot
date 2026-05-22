import asyncio

from providers import OllamaProvider


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class FakeSession:
    def __init__(self):
        self.payloads = []
        self.closed = False

    def post(self, url, json=None, timeout=None, headers=None):
        self.payloads.append(json)
        return FakeResponse()


def test_generate_chat_completion_model_override():
    provider = OllamaProvider("http://example.test", "base-model", 10, 0.5)
    provider.available = True
    session = FakeSession()
    provider._session = session

    async def run():
        message = await provider.generate_chat_completion(
            [{"role": "user", "content": "hi"}],
            model="rem-model",
            tools=[{"type": "function", "function": {"name": "ltm_list", "parameters": {"type": "object", "properties": {}}}}],
        )
        assert message["content"] == "ok"
    asyncio.run(run())
    assert session.payloads[0]["model"] == "rem-model"
    assert session.payloads[0]["tools"][0]["function"]["name"] == "ltm_list"
