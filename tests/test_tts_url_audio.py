import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.tts_voice.url_audio import download_reference_audio, validate_reference_url
from plugins.tts_voice.impl import TtsTool, synthesize_speech
from tooling.helpers import _SafeResolver


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://localhost/x', 'https://127.0.0.1/audio',
    'https://169.254.169.254/latest', 'https://[::1]/voice', 'http://10.1.2.3/file',
    'https://user:pass@example.com/x', 'http://x.internal/audio', 'http://[::ffff:127.0.0.1]/x',
    'http://[64:ff9b::7f00:1]/x', 'https://example.com:broken/a'])
def test_rejects_internal_and_credential_urls(url):
    with pytest.raises(ValueError):
        validate_reference_url(url)


class Response:
    def __init__(self, *, status=200, headers=None, body=b'ID3audio'):
        self.status = status
        self.headers = headers or {'Content-Type': 'audio/mpeg'}
        self.body = body
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass
    async def read(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def get(self, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        self.calls.append(url)
        return self.responses.pop(0)


def with_session(monkeypatch, responses):
    session = Session(responses)
    monkeypatch.setattr('plugins.tts_voice.url_audio._get_shared_session', AsyncMock(return_value=session))
    return session


def test_public_redirect_and_no_download_size_cap(monkeypatch):
    large = b'ID3' + b'x' * (9 * 1024 * 1024)
    session = with_session(monkeypatch, [Response(status=302, headers={'Location': 'https://audio.example.org/sample'}),
                                       Response(body=large)])
    result = asyncio.run(download_reference_audio('https://example.com/download'))
    assert result == large
    assert len(session.calls) == 2


def test_redirect_to_internal_is_rejected_before_connect(monkeypatch):
    session = with_session(monkeypatch, [Response(status=302, headers={'Location': 'http://127.0.0.1/secret'})])
    with pytest.raises(ValueError, match='public'):
        asyncio.run(download_reference_audio('https://example.com/clip'))
    assert len(session.calls) == 1


@pytest.mark.parametrize('response', [Response(status=404), Response(headers={'Content-Type': 'text/html'}), Response(body=b'')])
def test_source_errors_are_reported_without_url_or_secret(monkeypatch, response):
    with_session(monkeypatch, [response])
    with pytest.raises(ValueError) as exc:
        asyncio.run(download_reference_audio('https://example.com/file?token=secret'))
    assert 'secret' not in str(exc.value)


def test_private_dns_is_blocked_at_connection_time(monkeypatch):
    async def journey():
        resolver = _SafeResolver()
        resolver._resolver.resolve = AsyncMock(return_value=[{'host': '127.0.0.1'}])
        try:
            with pytest.raises(OSError, match='blocked unsafe'):
                await resolver.resolve('public.example.com')
        finally:
            await resolver.close()
    asyncio.run(journey())


def test_synthesis_keeps_audio_larger_than_old_limit(monkeypatch):
    large = b'ID3' + b'x' * (9 * 1024 * 1024)
    class Result:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return large
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: Result())
    assert synthesize_speech('hello', 'voice', 'key', 'model') == large
    assert 'maxLength' not in TtsTool.parameters['properties']['reference_audio_url']


def test_conflicting_references_rejected_before_network():
    result = asyncio.run(TtsTool(SimpleNamespace()).execute(SimpleNamespace(), text='hello', reference='clip', reference_audio_url='https://example.com/a'))
    assert 'choose one reference' in result
