"""M7-A S3.3-C3-B: observe / curl_import 公网接线测试。

三组覆盖：
1. route_permitted 纯函数矩阵——loopback 与原判式逐条等价对照；public 同源限定
   （比 allowlist 更严：子域/第三方/降级/非默认端口全部 abort）；
2. observe() 公网入口准入——check_target 与 SSRF 解析分类都在浏览器启动之前，
   异常路径零浏览器副作用（不启动 Playwright、不出网）；
3. parse_curl / observe_imported 公网流程——导入 query 作为页码证据被接受，
   MockTransport 离线重放，policy=None 时与旧行为逐字节一致。
"""

import asyncio
import json

import httpx
import pytest

from backend.app.pipeline.contracts import local_origin
from backend.app.pipeline.curl_import import observe_imported, parse_curl
from backend.app.pipeline.observe import observe, route_permitted
from backend.app.policy import (AllowRule, TargetPolicy, TargetPolicyError,
                                default_policy)

PUBLIC_POLICY = TargetPolicy(mode='public_http',
                             allowlist=[AllowRule(domain='example.com', include_subdomains=True)])
PAGE_1 = {'items': [{'id': 1, 'name': 'a'}], 'total': 1}


# --- 1. route_permitted 矩阵 ------------------------------------------------------

def legacy_permitted(url, origin):
    """改动前 route_request 内嵌判式的忠实复刻（等价性参照物）。"""
    try:
        return local_origin(url) == origin
    except ValueError:
        return False


LOOPBACK_ORIGIN = 'http://127.0.0.1:8000'
LOOPBACK_URLS = [
    'http://127.0.0.1:8000/api?page=2',
    'http://127.0.0.1:8000/',
    'http://127.0.0.1:8123/api',
    'http://localhost:8000/api',
    'https://127.0.0.1:8000/api',
    'http://example.com/api',
    'not a url',
    '',
]


@pytest.mark.parametrize('url', LOOPBACK_URLS)
@pytest.mark.parametrize('policy', [None, 'default'], ids=['none', 'loopback_policy'])
def test_route_permitted_loopback_equals_legacy(url, policy):
    effective = None if policy is None else default_policy()
    assert route_permitted(url, LOOPBACK_ORIGIN, effective) == legacy_permitted(url, LOOPBACK_ORIGIN)


@pytest.mark.parametrize('url,expected', [
    ('https://example.com/api?page=2', True),        # 同源（子请求 query 正常形态）
    ('https://example.com/', True),
    ('https://sub.example.com/api', False),          # 同源限定严于 allowlist：子域也拦
    ('https://evil.com/api', False),
    ('http://example.com/api', False),               # scheme 降级
    ('https://example.com:8443/api', False),         # 非默认端口
    ('https://user:pass@example.com/api', False),    # 凭证
    ('https://10.0.0.1/api', False),                 # 内网 IP
    ('not a url', False),
    ('', False),
])
def test_route_permitted_public_same_origin_only(url, expected):
    assert route_permitted(url, 'https://example.com', PUBLIC_POLICY) is expected


# --- 2. observe 公网入口准入（浏览器启动前，零副作用） ---------------------------------

def test_observe_public_entry_rejects_non_allowlisted_before_browser():
    with pytest.raises(TargetPolicyError) as excinfo:
        asyncio.run(observe('https://evil.com/list', policy=PUBLIC_POLICY))
    assert excinfo.value.code == 'target_not_in_allowlist'


def test_observe_public_entry_ssrf_admission_before_browser():
    with pytest.raises(TargetPolicyError) as excinfo:
        asyncio.run(observe('https://example.com/list', policy=PUBLIC_POLICY,
                            resolver=lambda host: ['10.0.0.1']))
    assert excinfo.value.code == 'ssrf_ip_blocked'


def test_observe_loopback_entry_behavior_unchanged():
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(observe('http://localhost:8000/list'))
    assert type(excinfo.value) is ValueError           # 裸 ValueError，不升级
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(observe('http://127.0.0.1:8000/list?page=1', policy=default_policy()))
    assert str(excinfo.value) == 'entry_url_query_not_supported'


# --- 3. parse_curl / observe_imported -----------------------------------------------

def test_parse_curl_public_allowed_with_policy():
    result = parse_curl('curl "https://example.com/api?page=1"', PUBLIC_POLICY)
    assert result['executable'] is True
    assert result['url'] == 'https://example.com/api?page=1'
    assert result['query'] == {'page': '1'}
    assert result['method'] == 'GET'


def test_parse_curl_public_post_json_allowed():
    text = """curl -X POST -H 'Content-Type: application/json' -d '{"page":1}' https://example.com/api"""
    result = parse_curl(text, PUBLIC_POLICY)
    assert result['executable'] is True
    assert result['method'] == 'POST'
    assert result['request_body'] == {'page': 1}


def test_parse_curl_public_url_without_policy_stays_rejected():
    """无 policy 时公网 cURL 维持现状拒绝（旧行为不变）。"""
    result = parse_curl('curl "https://example.com/api?page=1"')
    assert result['executable'] is False
    assert result['url'] is None
    assert result['reasons']


@pytest.mark.parametrize('text', [
    'curl https://evil.com/api',                                   # allowlist 外
    'curl http://example.com/api',                                 # 非 https
    'curl https://127.0.0.1:8000/api',                             # 公网策略下的 loopback
    'curl -H "Authorization: Bearer t" https://example.com/api',   # 敏感头照旧阻断
    'curl -H "Cookie: a=b" https://example.com/api',
    'curl "https://user:pass@example.com/api"',                    # 凭证
    'curl "https://example.com/api#frag"',                         # fragment
])
def test_parse_curl_public_rejections(text):
    result = parse_curl(text, PUBLIC_POLICY)
    assert result['executable'] is False
    assert result['url'] is None
    assert result['reasons']


def test_parse_curl_loopback_equivalence_none_vs_default_policy():
    text = 'curl http://127.0.0.1:8000/api?page=1'
    assert parse_curl(text) == parse_curl(text, None) == parse_curl(text, default_policy())
    assert parse_curl(text)['executable'] is True


def replay(handler, url, method='GET', request_body=None, policy=PUBLIC_POLICY):
    return asyncio.run(observe_imported(url, method, request_body, policy=policy,
                                        transport=httpx.MockTransport(handler)))


def test_observe_imported_public_get_replay():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=PAGE_1)

    observations = replay(handler, 'https://example.com/api?page=1')
    assert len(observations) == 1
    record = observations[0]
    assert record.request_id == 'req_001'
    assert record.url == 'https://example.com/api'        # query 从 url 剥离
    assert record.query == {'page': '1'}                  # 但作为证据保留
    assert record.method == 'GET' and record.body == PAGE_1
    assert [str(r.url) for r in seen] == ['https://example.com/api?page=1']


def test_observe_imported_public_post_replay_keeps_body():
    seen = []

    def handler(request):
        seen.append((request, request.read()))
        return httpx.Response(200, json=PAGE_1)

    body = {'page': 1, 'q': 'widget'}
    observations = replay(handler, 'https://example.com/api', 'POST', dict(body))
    assert observations[0].request_body == body
    request, raw = seen[0]
    assert request.method == 'POST'
    assert json.loads(raw) == body                        # body 原样重放，未被改写


def test_observe_imported_without_policy_keeps_loopback_behavior():
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(observe_imported('https://example.com/api', 'GET', None))
    assert type(excinfo.value) is ValueError              # 裸 ValueError，旧码原样
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'


def test_observe_imported_non_json_response_still_rejected():
    def handler(request):
        return httpx.Response(200, text='<html></html>')

    with pytest.raises(ValueError) as excinfo:
        replay(handler, 'https://example.com/api')
    assert str(excinfo.value) == 'expected_json'


def test_observe_imported_public_non_allowlisted_rejected():
    with pytest.raises(TargetPolicyError) as excinfo:
        replay(lambda request: httpx.Response(200, json=PAGE_1), 'https://evil.com/api')
    assert excinfo.value.code == 'target_not_in_allowlist'
