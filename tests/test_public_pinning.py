import asyncio

import httpx

from backend.app.policy.transport import PublicTransport


def test_transport_connects_only_to_checked_ip_preserving_tls_host(monkeypatch):
    seen = []

    async def send(self, request):
        seen.append(request)
        return httpx.Response(200, content=b'ok')

    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)

    async def check():
        async with PublicTransport(resolver=lambda host: ['93.184.216.34']) as transport:
            original = httpx.Request('GET', 'https://example.com/api?page=1')
            await transport.handle_async_request(original)
            assert original.url.host == 'example.com'

    asyncio.run(check())
    assert seen[0].url.host == '93.184.216.34'
    assert seen[0].headers['host'] == 'example.com'
    assert seen[0].extensions['sni_hostname'] == 'example.com'
    assert seen[0].url.query == b'page=1'


def test_public_transport_does_not_forward_cookie_jar_credentials(monkeypatch):
    sent = []
    async def send(self, request):
        sent.append(request)
        return httpx.Response(200)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    async def check():
        async with PublicTransport(resolver=lambda host: ['93.184.216.34']) as transport:
            await transport.handle_async_request(httpx.Request('GET', 'https://example.com/',
                headers={'Cookie': 'session=test', 'Authorization': 'Bearer test'}))
    asyncio.run(check())
    assert 'cookie' not in sent[0].headers
    assert 'authorization' not in sent[0].headers
