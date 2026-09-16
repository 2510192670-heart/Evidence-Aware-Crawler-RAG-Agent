import asyncio

import httpx
import pytest

from backend.app.policy import AllowRule, TargetPolicy
from backend.app.policy.robots import RobotsPolicy


def test_oversized_robots_is_rejected_without_partial_rule_interpretation():
    from backend.app.policy.robots import fetch_robots, ROBOTS_MAX_BYTES
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=' ' * (ROBOTS_MAX_BYTES + 1)))
    policy = asyncio.run(fetch_robots('https://example.com', transport))
    assert policy.fetch_succeeded is False


def test_browser_proxy_blocks_denied_paths_and_strips_headers():
    from backend.app.policy.browser import PublicBrowserProxy
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='hello', headers={'set-cookie': 'session=secret'})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = TargetPolicy(mode='public_http', allowlist=(AllowRule(domain='example.com'),))
            proxy = PublicBrowserProxy('https://example.com', policy, client,
                                       RobotsPolicy('User-agent: *\nDisallow: /private', fetch_succeeded=True))
            for url in ['https://example.com/private', 'https://evil.example/',
                        'https://example.com/?token=secret']:
                with pytest.raises(ValueError):
                    await proxy.fetch(url, 'GET')
            assert calls == []
            response = await proxy.fetch('https://example.com/list', 'GET')
            assert response['body'] == b'hello'
            assert 'set-cookie' not in response['headers']
            assert 'cookie' not in calls[0].headers
            assert 'authorization' not in calls[0].headers

    asyncio.run(check())


def test_real_browser_observation_uses_guarded_transport(monkeypatch):
    import importlib
    module = importlib.import_module('backend.app.pipeline.observe')
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /private')
        if request.url.path == '/':
            return httpx.Response(200, text='''<html><body><h1>Collection fixture</h1>
                <script>fetch('/api?page=1').then(r=>r.json());
                fetch('/private').catch(()=>{});</script></body></html>''',
                headers={'content-type': 'text/html'})
        if request.url.path == '/api':
            return httpx.Response(200, json={'items': [{'id': 1, 'name': 'verified'}], 'total': 1})
        return httpx.Response(404)

    monkeypatch.setattr(module, 'build_public_transport', lambda **kw: httpx.MockTransport(handler))
    policy = TargetPolicy(mode='public_http', allowlist=(AllowRule(domain='example.com'),))
    observations = asyncio.run(module.observe('https://example.com/', '', policy=policy,
                                             resolver=lambda host: ['93.184.216.34']))
    assert len(observations) == 1
    assert observations[0].body['items'][0]['name'] == 'verified'
    assert all(request.url.path != '/private' for request in requests)
    assert any(request.url.path == '/api' for request in requests)
