"""Strict subset of copied cURL (bash). Never execute shell text."""
import json
import shlex
from urllib.parse import urlsplit, parse_qsl

import httpx

from .contracts import local_origin, has_sensitive_keys, SENSITIVE, Observation


# 允许的请求方法；方法与请求体形态必须匹配：GET 无体，POST 必须是 JSON 对象。
SUPPORTED_METHODS = {'GET', 'POST'}
# curl 里携带请求体的旗标；只接受字面 JSON，@文件 一律拒绝。
DATA_FLAGS = {'-d', '--data', '--data-raw', '--data-binary'}


def checked_url(url):
    local_origin(url)
    if any(ord(c) < 33 for c in url) or '\\' in url:
        raise ValueError('invalid_url')
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    if len(dict(pairs)) != len(pairs) or any(SENSITIVE.search(k) for k, _ in pairs):
        raise ValueError('unsupported_query')
    return url


def _is_json_content_type(value: str) -> bool:
    """只有 application/json（允许 charset 等参数）才算 JSON 内容类型。"""
    return value.split(';', 1)[0].strip().lower() == 'application/json'


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
    explicit_method = False
    data = None
    content_type = None
    blocked_headers = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('-H', '--header', '-X', '--request', '--url') or arg in DATA_FLAGS:
            i += 1
            if i >= len(args):
                raise ValueError('missing_argument')
            value = args[i]
            if arg in ('-H', '--header'):
                name, sep, content = value.partition(':')
                if not sep or '\n' in value or '\r' in value:
                    raise ValueError('invalid_header')
                lowered = name.strip().lower()
                if lowered == 'accept':
                    if content.strip().lower() not in ('application/json', '*/*'):
                        blocked_headers = True
                elif lowered == 'content-type':
                    # 记下内容类型，方法确定后在最终校验里判定；不在这里放宽。
                    content_type = content.strip().lower()
                else:
                    blocked_headers = True
            elif arg in ('-X', '--request'):
                method = value.upper()
                explicit_method = True
            elif arg in DATA_FLAGS:
                if value.startswith('@'):
                    raise ValueError('unsupported_data_file')
                if data is not None:
                    raise ValueError('multiple_data_bodies')
                data = value
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
    if data is not None and not explicit_method:
        # curl 语义：携带 -d/--data 时默认方法为 POST。
        method = 'POST'
    reasons = []
    try:
        checked_url(url)
    except ValueError:
        reasons.append('仅支持本机 HTTP URL，且不能包含敏感参数或重复参数。')
    request_body = None
    if method not in SUPPORTED_METHODS:
        reasons.append('当前仅支持 GET 与 POST(JSON) 请求。')
    elif method == 'POST':
        if data is None:
            reasons.append('POST 导入必须携带 JSON 请求体（-d/--data）。')
        else:
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict) or not parsed:
                reasons.append('POST 请求体必须是 JSON 对象。')
            elif has_sensitive_keys(parsed):
                reasons.append('POST 请求体包含敏感字段，不会被导入。')
            elif content_type is None or not _is_json_content_type(content_type):
                reasons.append('POST 导入必须显式声明 Content-Type: application/json；表单与 multipart 不支持。')
            else:
                request_body = parsed
    elif data is not None:
        reasons.append('GET 请求不能携带请求体。')
    elif content_type is not None:
        # GET 携带 Content-Type 头不是 M3 观察到的形态，保持拒绝。
        blocked_headers = True
    if blocked_headers:
        reasons.append('包含尚不支持的请求头；Cookie、凭证及其他自定义头不会被导入。')
    if reasons:
        return {'executable': False, 'reasons': reasons,
                'method': method if method in SUPPORTED_METHODS else '不支持',
                'url': None, 'query': {}}
    result = {'executable': True, 'reasons': [], 'method': method, 'url': url,
              'query': dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))}
    if method == 'POST':
        result['request_body'] = request_body
        result['notes'] = ['仅接收 Accept: application/json 或 */* 与 Content-Type: application/json；'
                           '执行时以 JSON body 重放，只修改页码成员。--compressed 由 HTTP 客户端处理。']
    else:
        result['notes'] = ['仅接收 Accept: application/json 或 */*；执行时统一请求 JSON。--compressed 由 HTTP 客户端处理。']
    return result


async def observe_imported(url, method: str = 'GET', request_body=None):
    checked_url(url)
    if method not in SUPPORTED_METHODS:
        raise ValueError('unsupported_method')
    if method == 'POST':
        # 与观察边界一致：POST 证据必须是安全的非空 JSON 对象。
        if not isinstance(request_body, dict) or not request_body or has_sensitive_keys(request_body):
            raise ValueError('unsupported_request_body')
    elif request_body is not None:
        raise ValueError('get_must_not_carry_request_body')
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        if method == 'POST':
            stream = client.stream('POST', url, json=request_body, timeout=10,
                                   headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
        else:
            stream = client.stream('GET', url, headers={'Accept': 'application/json'}, timeout=10)
        async with stream as response:
            response.raise_for_status()
            if 'json' not in response.headers.get('content-type', ''):
                raise ValueError('expected_json')
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 256 * 1024:
                    raise ValueError('response_too_large')
    body = json.loads(raw)
    if not isinstance(body, (dict, list)):
        raise ValueError('expected_object_or_list')
    parts = urlsplit(url)
    return [Observation(request_id='req_001', url=parts._replace(query='').geturl(),
                        method=method, query=dict(parse_qsl(parts.query, keep_blank_values=True)),
                        request_body=request_body, body=body)]
