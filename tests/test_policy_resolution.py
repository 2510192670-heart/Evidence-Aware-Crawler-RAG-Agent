"""M7-A S3.3-C1: 公网执行前置安全基础层测试。

覆盖四个新模块（全部离线，禁止真实 DNS）：
1. resolution —— SSRF 解析守卫：危险段穷举矩阵（127.0.0.1 / 10.x / 192.168.x /
   172.16-31 / 169.254 / 100.64 CGNAT / ::1 / fc00:: / fe80:: / ff00:: /
   ::ffff:10.0.0.1 映射解包 / 198.18 benchmark / 6to4 / Teredo / 未知形态）、
   多 A 记录全检、解析失败码、fake resolver 注入；
2. loader —— strict JSON、非法字段拒绝、public https-only 约束；
3. transport —— 请求前守卫在连接建立之前拒绝（离线可证）、限速参数边界；
4. robots —— allow/disallow 判定、fail-closed 行为接口。
"""

import asyncio
import json

import httpx
import pytest

from backend.app.pipeline.errors import SPECS, Category, classify
from backend.app.policy import TargetPolicyError
from backend.app.policy.loader import load_policy_file
from backend.app.policy.resolution import classify_ip_blocked, resolve_and_classify
from backend.app.policy.robots import RobotsPolicy
from backend.app.policy.transport import PublicTransport, build_public_transport


def fake_resolver(mapping):
    """hostname → IP 列表；未登记主机返回空列表（= 解析失败语义）。"""
    def resolve(hostname):
        return list(mapping.get(hostname, []))
    return resolve


# --- 1. resolution：危险地址矩阵 ------------------------------------------------

BLOCKED_IPS = [
    # IPv4 loopback / private / link-local / CGNAT / benchmark / reserved
    '127.0.0.1', '127.255.255.254',
    '10.0.0.1', '10.255.255.255',
    '192.168.0.1', '192.168.255.255',
    '172.16.0.1', '172.31.255.255',
    '169.254.169.254',           # 云 metadata 端点
    '100.64.0.1', '100.127.255.255',   # CGNAT
    '198.18.0.1',                # benchmark
    '0.0.0.0', '255.255.255.255',
    '224.0.0.1', '240.0.0.1',
    '192.0.2.1', '198.51.100.1', '203.0.113.1',   # TEST-NET
    # IPv6
    '::1', '::',
    'fc00::1', 'fd12:3456:789a::1',     # ULA fc00::/7
    'fe80::1', 'fe80::1%eth0',          # link-local（含 scope id 形态）
    'ff02::1',                          # multicast
    # IPv4-mapped IPv6：解包内层再分类
    '::ffff:10.0.0.1', '::ffff:127.0.0.1', '::ffff:169.254.1.1',
    # 6to4 / Teredo（内嵌 v4 可携带私网，整段拒绝）
    '2002:7f00:0001::1', '2001:0000:4136:e378:8000:63bf:3fff:fde2',
    # 未知形态 fail-closed
    '999.999.999.999', 'not-an-ip', '',
]


@pytest.mark.parametrize('value', BLOCKED_IPS)
def test_blocked_ip_classification(value):
    assert classify_ip_blocked(value) is True


@pytest.mark.parametrize('value', [
    '93.184.216.34', '8.8.8.8', '1.1.1.1', '172.15.0.1', '172.32.0.1',
    '100.63.255.255', '100.128.0.1', '198.17.255.255', '11.0.0.1',
    '2606:2800:220:1:248:1893:25c8:1946',
])
def test_public_ip_classification(value):
    assert classify_ip_blocked(value) is False


def test_resolve_and_classify_allows_public_and_normalizes():
    resolver = fake_resolver({'example.com': ['93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946']})
    ips = resolve_and_classify('example.com', resolver)
    assert ips == ('93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946')


def test_resolve_and_classify_checks_every_record():
    """任一 A/AAAA 记录命中危险段即整体拒绝——不是抽查第一条。"""
    resolver = fake_resolver({'mixed.example': ['93.184.216.34', '10.0.0.8']})
    with pytest.raises(TargetPolicyError) as excinfo:
        resolve_and_classify('mixed.example', resolver)
    assert excinfo.value.code == 'ssrf_ip_blocked'
    resolver_tail = fake_resolver({'mixed.example': ['127.0.0.1', '93.184.216.34']})
    with pytest.raises(TargetPolicyError) as excinfo:
        resolve_and_classify('mixed.example', resolver_tail)
    assert excinfo.value.code == 'ssrf_ip_blocked'


def test_resolve_and_classify_failure_codes():
    with pytest.raises(TargetPolicyError) as excinfo:
        resolve_and_classify('no-such-host.invalid', fake_resolver({}))
    assert excinfo.value.code == 'target_resolution_failed'

    def exploding_resolver(hostname):
        raise RuntimeError('dns backend exploded')

    with pytest.raises(TargetPolicyError) as excinfo:
        resolve_and_classify('example.com', exploding_resolver)
    # resolver 的任何异常都不能变成放行理由。
    assert excinfo.value.code == 'target_resolution_failed'


def test_resolve_and_classify_never_uses_real_dns():
    """注入 resolver 时不得触达 socket（离线保证：假解析结果被原样分类）。"""
    resolver = fake_resolver({'c1.test': ['192.168.1.1']})
    with pytest.raises(TargetPolicyError) as excinfo:
        resolve_and_classify('c1.test', resolver)
    assert excinfo.value.code == 'ssrf_ip_blocked'


def test_resolution_codes_classification():
    """S3.3-C2：解析守卫两码已注册，classify 类别/策略钉死。"""
    ssrf = classify(TargetPolicyError('ssrf_ip_blocked'))
    assert ssrf.category is Category.SECURITY
    assert ssrf.repairable is False and ssrf.retryable is False
    resolution = classify(TargetPolicyError('target_resolution_failed'))
    assert resolution.category is Category.TRANSPORT
    assert resolution.repairable is False
    # retryable 精确集合不变：解析失败不自动重试。
    assert resolution.retryable is False
    assert {code for code, spec in SPECS.items() if spec.retryable} == {
        'connect_timeout', 'network_error'}


# --- 2. loader -------------------------------------------------------------------

def write_policy(tmp_path, document, name='policy.json'):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def test_loader_accepts_valid_public_policy(tmp_path):
    path = write_policy(tmp_path, {
        'mode': 'public_http',
        'allowlist': [{'domain': 'example.com', 'include_subdomains': True}],
        'schemes': ['https'],
    })
    policy = load_policy_file(path)
    assert policy.mode == 'public_http'
    assert policy.allowlist[0].domain == 'example.com'


def test_loader_accepts_loopback_policy(tmp_path):
    policy = load_policy_file(write_policy(tmp_path, {'mode': 'loopback'}))
    assert policy.mode == 'loopback'


def test_loader_rejects_http_scheme_for_public(tmp_path):
    path = write_policy(tmp_path, {
        'mode': 'public_http',
        'allowlist': [{'domain': 'example.com'}],
        'schemes': ['https', 'http'],
    })
    with pytest.raises(TargetPolicyError) as excinfo:
        load_policy_file(path)
    assert excinfo.value.code == 'invalid_policy'


@pytest.mark.parametrize('document', [
    {'mode': 'public_http'},                                          # 无 allowlist
    {'mode': 'public_http', 'allowlist': [{'domain': '127.0.0.1'}]},  # IP 入册
    {'mode': 'public_http', 'allowlist': [{'domain': 'example.com'}], 'unknown': 1},
    {'mode': 'loopback', 'allowlist': [{'domain': 'example.com'}]},   # mode 组合非法
    {'mode': 'internet'},                                             # 闭集外
])
def test_loader_rejects_invalid_documents(tmp_path, document):
    with pytest.raises(TargetPolicyError) as excinfo:
        load_policy_file(write_policy(tmp_path, document))
    assert excinfo.value.code == 'invalid_policy'


def test_loader_rejects_unreadable_and_malformed_files(tmp_path):
    with pytest.raises(TargetPolicyError) as excinfo:
        load_policy_file(tmp_path / 'missing.json')
    assert excinfo.value.code == 'policy_file_not_readable'
    broken = tmp_path / 'broken.json'
    broken.write_text('{not json', encoding='utf-8')
    with pytest.raises(TargetPolicyError) as excinfo:
        load_policy_file(broken)
    assert excinfo.value.code == 'invalid_policy_file'
    scalar = write_policy(tmp_path, ['not', 'a', 'dict'], name='scalar.json')
    with pytest.raises(TargetPolicyError) as excinfo:
        load_policy_file(scalar)
    assert excinfo.value.code == 'invalid_policy_file'


# --- 3. transport（接口 + 请求前守卫，离线可证） ------------------------------------

def test_transport_blocks_before_connection():
    """守卫在建立连接之前拒绝：blocked resolver 下 handle_async_request 必抛，
    且不会发起任何网络 IO（异常先于 super() 到达）。"""
    transport = build_public_transport(resolver=fake_resolver({'example.com': ['10.0.0.1']}))
    request = httpx.Request('GET', 'https://example.com/items')
    with pytest.raises(TargetPolicyError) as excinfo:
        asyncio.run(transport.handle_async_request(request))
    assert excinfo.value.code == 'ssrf_ip_blocked'


def test_transport_pre_request_checks_revalidate_each_call():
    """软 pinning 语义：每次请求前都重新解析分类（rebinding 剧本）。"""
    answers = {'example.com': [['93.184.216.34'], ['127.0.0.1']]}
    calls = {'n': 0}

    def rebinding_resolver(hostname):
        batch = answers[hostname][min(calls['n'], 1)]
        calls['n'] += 1
        return batch

    transport = PublicTransport(resolver=rebinding_resolver)
    assert transport.pre_request_checks('example.com') == ('93.184.216.34',)
    with pytest.raises(TargetPolicyError) as excinfo:
        transport.pre_request_checks('example.com')     # DNS 已翻转为私网
    assert excinfo.value.code == 'ssrf_ip_blocked'


def test_transport_rps_bounds():
    with pytest.raises(ValueError):
        PublicTransport(per_domain_rps=0)
    with pytest.raises(ValueError):
        PublicTransport(per_domain_rps=2.5)     # 硬上限 2 rps
    assert isinstance(build_public_transport(), httpx.AsyncBaseTransport)


# --- 4. robots --------------------------------------------------------------------

ROBOTS_TEXT = '\n'.join([
    'User-agent: *',
    'Disallow: /private',
    'Allow: /public',
    '',
])


def test_robots_allow_disallow_judgement():
    policy = RobotsPolicy(ROBOTS_TEXT, fetch_succeeded=True)
    assert policy.is_allowed('/public/list') is True
    assert policy.is_allowed('/private/data') is False
    with pytest.raises(TargetPolicyError) as excinfo:
        policy.enforce('/private/data')
    assert excinfo.value.code == 'robots_disallowed'
    policy.enforce('/public/list')          # 不抛


def test_robots_fail_closed_on_missing_or_failed_fetch():
    assert RobotsPolicy().is_allowed('/anything') is False
    assert RobotsPolicy(None, fetch_succeeded=False).is_allowed('/anything') is False
    with pytest.raises(TargetPolicyError):
        RobotsPolicy(fetch_succeeded=False).enforce('/anything')


def test_robots_absent_file_means_allowed():
    """抓取成功且 robots.txt 不存在（404 → 空文本）：无规则 = 允许（RFC 9309 惯例）。"""
    policy = RobotsPolicy('', fetch_succeeded=True)
    assert policy.is_allowed('/anything') is True


def test_robots_rejects_non_path_and_bad_text():
    policy = RobotsPolicy(ROBOTS_TEXT, fetch_succeeded=True)
    assert policy.is_allowed('private/data') is False       # 非绝对路径形态
    broken = RobotsPolicy(b'bytes-not-str', fetch_succeeded=True)
    assert broken.is_allowed('/public') is False            # 非法文本 → fail-closed
