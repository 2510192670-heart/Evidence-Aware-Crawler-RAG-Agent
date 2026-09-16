"""Serve public browser requests through the checked HTTP transport."""
from urllib.parse import urlsplit

import httpx

from ..pipeline.curl_import import checked_url
from ..pipeline.contracts import has_sensitive_keys
from .errors import TargetPolicyError


class PublicBrowserProxy:
    def __init__(self, origin, policy, client, robots):
        self.origin, self.policy, self.client, self.robots = origin, policy, client, robots
        self.requests = 0

    async def fetch(self, url, method, body=None):
        checked_url(url, self.policy)
        parts = urlsplit(url)
        if f'{parts.scheme}://{parts.netloc}' != self.origin:
            raise TargetPolicyError('cross_origin_request')
        self.robots.enforce(parts.path + ('?' + parts.query if parts.query else ''))
        if method not in {'GET', 'POST'}:
            raise ValueError('unsupported_method')
        if method == 'POST' and (not isinstance(body, dict) or not body or has_sensitive_keys(body)):
            raise ValueError('unsupported_request_body')
        if method == 'GET' and body is not None:
            raise ValueError('get_must_not_carry_request_body')
        self.requests += 1
        if self.requests > 80:
            raise ValueError('browser_request_budget_exceeded')
        # Build explicitly: neither the browser headers nor the client's cookie
        # jar may become authentication replay.
        request = httpx.Request(method, url, json=body if method == 'POST' else None,
                                headers={'User-Agent': 'WebDataAgent'},
                                extensions={'timeout': dict(connect=10, read=10, write=10, pool=10)})
        response = await self.client.send(request, stream=True, follow_redirects=False)
        try:
            if 300 <= response.status_code < 400:
                raise ValueError('public_redirect_not_supported')
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('response_too_large')
            headers = {key: value for key, value in response.headers.items()
                       if key in {'content-type', 'content-security-policy'}}
            return {'status': response.status_code, 'headers': headers, 'body': bytes(raw)}
        finally:
            await response.aclose()
