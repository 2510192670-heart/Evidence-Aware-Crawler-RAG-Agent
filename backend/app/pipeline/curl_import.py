"""Strict subset of copied cURL (bash). Never execute shell text."""
import shlex
from urllib.parse import urlsplit, parse_qsl

import httpx

from .contracts import local_origin, SENSITIVE, Observation


def checked_url(url):
    local_origin(url)
    if any(ord(c) < 33 for c in url) or '\\' in url:
        raise ValueError('invalid_url')
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    if len(dict(pairs)) != len(pairs) or any(SENSITIVE.search(k) for k, _ in pairs):
        raise ValueError('unsupported_query')
    return url


def parse_curl(text):
    if len(text.encode('utf-8')) > 16384:
        raise ValueError('curl_too_large')
    # Conservative rejection also inside quotes; no expansion or local-file options.
    if any(c in text for c in ('`', '$', ';', '|', '\x00', '^')):
        raise ValueError('unsupported_shell_syntax')
    args = shlex.split(text.replace('\\\r\n', '').replace('\\\n', ''), posix=True)
    if not args or args.pop(0) not in ('curl', 'curl.exe'):
        raise ValueError('expected_curl')
    url = None
    method = 'GET'
    blocked_headers = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('-H', '--header', '-X', '--request', '--url'):
            i += 1
            if i >= len(args):
                raise ValueError('missing_argument')
            value = args[i]
            if arg in ('-H', '--header'):
                name, sep, content = value.partition(':')
                if not sep or '\n' in value or '\r' in value:
                    raise ValueError('invalid_header')
                if name.strip().lower() != 'accept' or content.strip().lower() not in ('application/json', '*/*'):
                    blocked_headers = True
            elif arg in ('-X', '--request'):
                method = value.upper()
            else:
                if url is not None:
                    raise ValueError('multiple_urls')
                url = value
        elif arg == '--compressed':
            pass
        elif arg.startswith('-') or url is not None:
            raise ValueError('unsupported_option_or_multiple_urls')
        else:
            url = arg
        i += 1
    if not url:
        raise ValueError('missing_url')
    reasons = []
    try:
        checked_url(url)
    except ValueError:
        reasons.append('仅支持本机 HTTP URL，且不能包含敏感参数或重复参数。')
    if method != 'GET':
        reasons.append('当前仅支持 GET 请求。')
    if blocked_headers:
        reasons.append('包含尚不支持的请求头；Cookie、凭证及其他自定义头不会被导入。')
    if reasons:
        return {'executable': False, 'reasons': reasons, 'method': 'GET' if method == 'GET' else '不支持', 'url': None, 'query': {}}
    return {'executable': True, 'reasons': [], 'method': 'GET', 'url': url,
            'query': dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)),
            'notes': ['仅接收 Accept: application/json 或 */*；执行时统一请求 JSON。--compressed 由 HTTP 客户端处理。']}


async def observe_imported(url):
    checked_url(url)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        async with client.stream('GET', url, headers={'Accept': 'application/json'}, timeout=10) as response:
            response.raise_for_status()
            if 'json' not in response.headers.get('content-type', ''):
                raise ValueError('expected_json')
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 256 * 1024:
                    raise ValueError('response_too_large')
    import json
    body = json.loads(raw)
    if not isinstance(body, (dict, list)):
        raise ValueError('expected_object_or_list')
    parts = urlsplit(url)
    return [Observation(request_id='req_001', url=parts._replace(query='').geturl(),
                        query=dict(parse_qsl(parts.query, keep_blank_values=True)), body=body)]
