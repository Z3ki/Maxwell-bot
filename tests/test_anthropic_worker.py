"""Anthropic worker requests use the Messages protocol without crossing credentials."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web

from maxwell_core.providers.errors import (
    ProviderAuthenticationError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
)
from maxwell_core.providers.factory import openai_compat_provider
from plugins.maxwell_extras.worker_providers import select_worker_provider


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_post('/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


def anthropic_response(content, stop_reason='end_turn', usage=None):
    return {
        'id': 'msg_1', 'type': 'message', 'role': 'assistant',
        'model': 'claude-haiku-5-5', 'content': content,
        'stop_reason': stop_reason,
        'usage': usage or {'input_tokens': 10, 'output_tokens': 3},
    }


def make_provider(base, **kwargs):
    return openai_compat_provider(
        base_url=base + '/v1', model='claude-haiku-5-5', api_key='anthropic-secret',
        api_format='anthropic', max_tokens=1024, retry_attempts=1,
        empty_response_retries=0, **kwargs,
    )


def test_anthropic_system_tools_parallel_calls_and_tool_results():
    async def run():
        requests = []

        async def handle(request):
            requests.append((request.path, dict(request.headers), await request.json()))
            return web.json_response(anthropic_response([
                {'type': 'text', 'text': 'Checking.'},
                {'type': 'tool_use', 'id': 'call_a', 'name': 'lookup', 'input': {'q': 'one'}},
                {'type': 'tool_use', 'id': 'call_b', 'name': 'lookup', 'input': {'q': 'two'}},
            ], 'tool_use'))

        async with server(handle) as base:
            provider = make_provider(base)
            provider.available = True
            try:
                messages = [
                    {'role': 'system', 'content': 'You are a worker.'},
                    {'role': 'user', 'content': 'Do the task'},
                    {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'old_a', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"old"}'}},
                        {'id': 'old_b', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"bad"}'}},
                    ]},
                    {'role': 'tool', 'tool_call_id': 'old_a', 'content': 'found'},
                    {'role': 'tool', 'tool_call_id': 'old_b', 'content': 'failed', 'is_error': True},
                ]
                tools = [{'type': 'function', 'function': {
                    'name': 'lookup', 'description': 'Look things up',
                    'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q']},
                }}]
                result = await provider.generate_response(messages, tools=tools, stream=False)
                assert result.content == 'Checking.'
                assert [(c['id'], c['function']['name'], json.loads(c['function']['arguments']))
                        for c in result.tool_calls] == [
                    ('call_a', 'lookup', {'q': 'one'}), ('call_b', 'lookup', {'q': 'two'})]
                path, headers, payload = requests[0]
                assert path == '/v1/messages'
                assert headers['x-api-key'] == 'anthropic-secret'
                assert headers['anthropic-version']
                assert 'Authorization' not in headers
                assert payload['model'] == 'claude-haiku-5-5'
                assert payload['max_tokens'] == 1024
                assert payload['system'] == [{'type': 'text', 'text': 'You are a worker.'}]
                assert payload['tools'] == [{'name': 'lookup', 'description': 'Look things up',
                    'input_schema': tools[0]['function']['parameters']}]
                wire = payload['messages']
                assert all(m['role'] != 'system' for m in wire)
                blocks = [b for m in wire for b in (m['content'] if isinstance(m['content'], list) else [])]
                assert {'type': 'tool_use', 'id': 'old_a', 'name': 'lookup', 'input': {'q': 'old'}} in blocks
                assert any(b.get('type') == 'tool_result' and b.get('tool_use_id') == 'old_a' and b.get('content') == [{'type': 'text', 'text': 'found'}] for b in blocks)
                assert any(b.get('type') == 'tool_result' and b.get('tool_use_id') == 'old_b' and b.get('content') == [{'type': 'text', 'text': 'failed'}] and b.get('is_error') is True for b in blocks)
                assert messages[2]['tool_calls'][0]['function']['arguments'] == '{"q":"old"}'
            finally:
                await provider.close()

    asyncio.run(run())


def test_anthropic_usage_includes_cache_reads_and_writes():
    async def run():
        async def handle(request):
            return web.json_response(anthropic_response(
                [{'type': 'text', 'text': 'done'}], usage={
                    'input_tokens': 20, 'output_tokens': 7,
                    'cache_read_input_tokens': 30, 'cache_creation_input_tokens': 10,
                }))

        async with server(handle) as base:
            provider = make_provider(base)
            provider.available = True
            try:
                result = await provider.generate_response([{'role': 'user', 'content': 'work'}], stream=False)
                assert result == 'done'
                assert result.usage['prompt_tokens'] == 60
                assert result.usage['completion_tokens'] == 7
                assert result.usage['total_tokens'] == 67
                assert result.usage['cached_tokens'] == 30
                assert result.usage['cache_write_tokens'] == 10
            finally:
                await provider.close()

    asyncio.run(run())


@pytest.mark.parametrize('status,error_type', [
    (400, ProviderInvalidRequestError),
    (401, ProviderAuthenticationError),
    (429, ProviderRateLimitError),
])
def test_anthropic_http_failures_use_provider_errors(status, error_type):
    async def run():
        async def handle(request):
            return web.json_response({'type': 'error', 'error': {'type': 'api_error', 'message': 'upstream failure'}}, status=status)

        async with server(handle) as base:
            provider = make_provider(base)
            provider.available = True
            try:
                with pytest.raises(error_type):
                    await provider.generate_response([{'role': 'user', 'content': 'work'}], stream=False)
            finally:
                await provider.close()

    asyncio.run(run())


def test_anthropic_worker_falls_back_to_openai_without_key_leakage():
    async def run():
        requests = []

        async def handle(request):
            requests.append((request.path, dict(request.headers), await request.json()))
            if request.path == '/anthropic/v1/messages':
                return web.json_response({'type': 'error', 'error': {'type': 'authentication_error', 'message': 'bad key'}}, status=401)
            return web.json_response({'choices': [{'message': {'role': 'assistant', 'content': 'fallback answer'}, 'finish_reason': 'stop'}]})

        async with server(handle) as base:
            bot = SimpleNamespace(
                _make_chat_provider=openai_compat_provider,
                config=SimpleNamespace(
                    AI_WORKER_BASE_URL=base + '/anthropic/v1', AI_WORKER_MODEL='claude-haiku-5-5',
                    AI_WORKER_API_KEY='anthropic-secret', AI_WORKER_API_FORMAT='anthropic',
                    AI_FALLBACK_BASE_URL=base + '/openai/v1', AI_FALLBACK_MODEL='backup',
                    AI_FALLBACK_API_KEY='openai-secret', AI_WORKER_FALLBACK_API_FORMAT='openai',
                ),
            )
            provider, owned = await select_worker_provider(bot, 'user')
            try:
                assert owned
                for client in provider.providers:
                    client.available = True
                result = await provider.generate_response([{'role': 'user', 'content': 'work'}], stream=False)
                assert result == 'fallback answer'
                assert [item[0] for item in requests] == ['/anthropic/v1/messages', '/openai/v1/chat/completions']
                assert requests[0][1]['x-api-key'] == 'anthropic-secret'
                assert 'Authorization' not in requests[0][1]
                assert requests[1][1]['Authorization'] == 'Bearer openai-secret'
                assert 'x-api-key' not in requests[1][1]
                assert requests[1][2]['model'] == 'backup'
            finally:
                await provider.close()

    asyncio.run(run())


@pytest.mark.parametrize('reason,expected', [
    ('end_turn', 'stop'), ('stop_sequence', 'stop'),
    ('max_tokens', 'length'), ('tool_use', 'tool_calls'),
])
def test_anthropic_stop_reasons_translate_to_completion_reasons(reason, expected):
    from maxwell_core.providers.anthropic import translate_anthropic_response

    result = translate_anthropic_response(anthropic_response(
        [{'type': 'text', 'text': 'first'}, {'type': 'text', 'text': ' second'}], reason))
    assert result['choices'][0]['finish_reason'] == expected
    assert result['choices'][0]['message']['content'] == 'first second'


@pytest.mark.parametrize('response', [
    {}, {'content': 'not a list'}, {'content': [None]},
    {'content': [{'type': 'text', 'text': 123}]},
    {'content': [{'type': 'tool_use', 'id': 'x', 'name': 'lookup', 'input': 'not an object'}]},
])
def test_malformed_anthropic_response_is_not_accepted(response):
    from maxwell_core.providers.anthropic import translate_anthropic_response
    from maxwell_core.providers.protocol import ProviderProtocolError

    with pytest.raises(ProviderProtocolError):
        translate_anthropic_response(response)


@pytest.mark.parametrize('alternate_url,alternate_key,expected_key', [
    (None, None, 'worker-secret'),
    ('https://worker.example/v1', None, 'worker-secret'),
    ('https://other.example/v1', 'other-secret', 'other-secret'),
    (None, '', ''),
])
def test_worker_dedicated_fallback_credential_scope(alternate_url, alternate_key, expected_key):
    captured = {}
    client = SimpleNamespace()
    config = SimpleNamespace(
        AI_WORKER_BASE_URL='https://worker.example/v1', AI_WORKER_MODEL='claude-haiku-5-5',
        AI_WORKER_API_KEY='worker-secret', AI_WORKER_API_FORMAT='anthropic',
        AI_WORKER_FALLBACK_MODEL='backup', AI_WORKER_FALLBACK_API_FORMAT='anthropic',
    )
    if alternate_url is not None:
        config.AI_WORKER_FALLBACK_BASE_URL = alternate_url
    if alternate_key is not None:
        config.AI_WORKER_FALLBACK_API_KEY = alternate_key
    bot = SimpleNamespace(config=config, _make_chat_provider=lambda **kw: captured.update(kw) or client)
    assert asyncio.run(select_worker_provider(bot, 'user')) == (client, True)
    assert captured['api_format'] == 'anthropic'
    assert captured['fallback_api_format'] == 'anthropic'
    assert captured['fallback_model'] == 'backup'
    assert captured['fallback_base_url'] == (alternate_url or config.AI_WORKER_BASE_URL)
    assert captured['fallback_api_key'] == expected_key


def test_worker_fallback_different_endpoint_requires_explicit_key():
    bot = SimpleNamespace(config=SimpleNamespace(
        AI_WORKER_BASE_URL='https://worker.example/v1',
        AI_WORKER_MODEL='claude-haiku-5-5',
        AI_WORKER_API_KEY='worker-secret',
        AI_WORKER_FALLBACK_MODEL='backup',
        AI_WORKER_FALLBACK_BASE_URL='https://other.example/v1',
    ))
    with pytest.raises(ValueError, match='AI_WORKER_FALLBACK_API_KEY'):
        asyncio.run(select_worker_provider(bot, 'user'))


def test_anthropic_stream_reassembles_parallel_tool_json_and_usage():
    async def run():
        events = [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'cache_read_input_tokens': 4}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Working'}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': ' now'}},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'a', 'name': 'lookup', 'input': {}}},
            {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'b', 'name': 'lookup', 'input': {}}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"q":'}},
            {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"q":"two"}'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '"one"}'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 6}},
            {'type': 'message_stop'},
        ]
        async def handle(request):
            payload = await request.json()
            assert payload['stream'] is True
            body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
            return web.Response(text=body, content_type='text/event-stream')

        async with server(handle) as base:
            provider = make_provider(base)
            tokens = []
            tool_names = []
            async def on_tool(name, reasoning):
                tool_names.append((name, reasoning))

            provider.available = True
            try:
                result = await provider.generate_response(
                    [{'role': 'user', 'content': 'work'}], stream=True,
                    on_token=tokens.append, on_tool_call_name=on_tool)
                await asyncio.sleep(0)
                assert result.content == 'Working now'
                assert [token['content'] for token in tokens if token['content']] == ['Working', ' now']
                assert [token['tool_name'] for token in tokens if token['tool_name']] == ['lookup', 'lookup']
                assert tool_names == [('lookup', ''), ('lookup', '')]
                assert [(call['id'], json.loads(call['function']['arguments'])) for call in result.tool_calls] == [
                    ('a', {'q': 'one'}), ('b', {'q': 'two'})]
                assert result.usage['prompt_tokens'] == 14
                assert result.usage['cached_tokens'] == 4
                assert result.usage['completion_tokens'] == 6
            finally:
                await provider.close()

    asyncio.run(run())


def test_anthropic_truncated_stream_fails_instead_of_returning_partial_output():
    from maxwell_core.providers.protocol import ProviderProtocolError

    async def run():
        async def handle(request):
            events = [
                {'type': 'message_start', 'message': {'usage': {'input_tokens': 3}}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'partial'}},
            ]
            body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
            return web.Response(text=body, content_type='text/event-stream')

        async with server(handle) as base:
            provider = make_provider(base)
            provider.available = True
            try:
                with pytest.raises(ProviderProtocolError, match='incomplete'):
                    await provider.generate_response([{'role': 'user', 'content': 'work'}], stream=True)
            finally:
                await provider.close()

    asyncio.run(run())
