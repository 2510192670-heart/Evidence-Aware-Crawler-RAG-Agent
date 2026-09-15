"""M7-A S1: TargetPolicy 基础层契约测试。

验证三件事：
1. loopback 模式（含 policy=None）check_target 逐字节委托 local_origin，
   包括其裸 ValueError 行为——既有调用方零漂移；
2. public_http 模式的 allowlist/scheme/端口/query 判定确定性且 fail-closed；
3. 冻结边界：backend 受 v0.5.4-freeze 字节级冻结，S1 不扩展 errors.SPECS；
   新码经 classify() fail-closed 为 UNCLASSIFIED，复用既有码时分类照常生效。

本测试不发起任何网络请求、不做 DNS 解析、不依赖 fixtures、不修改任何冻结文件。
"""

import re

import pytest
from pydantic import ValidationError

from backend.app.pipeline.contracts import local_origin
from backend.app.pipeline.errors import SPECS, Category, classify
from backend.app.policy import (
    AllowRule,
    TargetPolicy,
    TargetPolicyError,
    check_target,
    default_policy,
    policy_sha256,
)

NEW_CODES = ('target_not_in_allowlist', 'public_scheme_not_allowed', 'unsupported_port')
REUSED_CODES = ('invalid_url', 'entry_url_query_not_supported')


def public_policy(**overrides) -> TargetPolicy:
    base = dict(mode='public_http',
                allowlist=[AllowRule(domain='example.com', include_subdomains=True)])
    base.update(overrides)
    return TargetPolicy(**base)


# --- 默认策略与 loopback 委托 ------------------------------------------------

def test_default_policy_is_loopback():
    policy = default_policy()
    assert policy.mode == 'loopback'
    assert policy.target_kind == 'http'
    assert policy.schema_version == 1
    assert policy.allowlist == ()
    assert policy.schemes == ('https',)
    assert policy.entry_url_query_allowed is False


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8000/x',
    'http://127.0.0.1:8123/a/b?page=2',
    'http://[::1]:9000/',
])
def test_loopback_delegation_matches_local_origin(url):
    assert check_target(url) == local_origin(url)
    assert check_target(url, default_policy()) == local_origin(url)


@pytest.mark.parametrize('url', [
    'https://127.0.0.1/x',          # scheme 非 http
    'http://localhost:8000/',        # 非字面量 loopback
    'http://example.com/',           # 公网
    'http://user@127.0.0.1/',        # 凭证
    'http://127.0.0.1/#frag',        # fragment
])
def test_loopback_delegation_preserves_bare_valueerror(url):
    """委托必须保留 local_origin 的裸 ValueError 行为，不得升级为 PipelineError。"""
    with pytest.raises(ValueError) as excinfo:
        check_target(url)
    assert type(excinfo.value) is ValueError
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'


# --- 模型契约：frozen / extra=forbid / mode 组合 ------------------------------

def test_policy_is_frozen_and_extra_forbidden():
    policy = default_policy()
    with pytest.raises(ValidationError):
        policy.mode = 'public_http'
    with pytest.raises(ValidationError):
        TargetPolicy(unknown_field=1)
    with pytest.raises(ValidationError):
        AllowRule(domain='example.com', unknown=1)


def test_loopback_mode_rejects_allowlist():
    with pytest.raises(ValidationError):
        TargetPolicy(allowlist=[AllowRule(domain='example.com')])


def test_public_mode_requires_allowlist_and_schemes():
    with pytest.raises(ValidationError):
        TargetPolicy(mode='public_http')
    with pytest.raises(ValidationError):
        TargetPolicy(mode='public_http', allowlist=[AllowRule(domain='example.com')],
                     schemes=[])


def test_browser_target_kind_is_declarable():
    policy = public_policy(target_kind='browser')
    assert policy.target_kind == 'browser'


# --- AllowRule 域名形态 -------------------------------------------------------

@pytest.mark.parametrize('domain', [
    'EXAMPLE.com',       # 大写（必须预先小写/IDNA 编码）
    'example.com.',      # 尾点
    '.example.com',      # 前导点（子域用 include_subdomains 表达）
    'localhost',         # 单标签
    '-example.com',      # 标签以连字符开头
    'example-.com',      # 标签以连字符结尾
    'exa_mple.com',      # 非法字符
    '127.0.0.1',         # IPv4 字面量
    '::1',               # IPv6 字面量
    '2130706433',        # 十进制 IP 变体（单标签亦拒绝）
    '',                  # 空
    'a' * 64 + '.com',   # 标签超长
    '.'.join(['a' * 63] * 5),  # 总长超 253
])
def test_allow_rule_rejects_malformed_domains(domain):
    with pytest.raises(ValidationError):
        AllowRule(domain=domain)


@pytest.mark.parametrize('domain', ['example.com', 'sub.a.example.com', 'xn--fiqs8s.example'])
def test_allow_rule_accepts_valid_domains(domain):
    assert AllowRule(domain=domain).domain == domain


# --- public_http 判定：接受路径 ------------------------------------------------

def test_public_accepts_allowlisted_and_normalizes_origin():
    policy = public_policy()
    assert check_target('https://example.com/a/b', policy) == 'https://example.com'
    # origin 返回实际主机（与 local_origin 语义一致），不折叠到规则根域。
    assert check_target('https://sub.example.com/', policy) == 'https://sub.example.com'
    # hostname 大小写折叠后匹配，origin 返回规范化小写。
    assert check_target('https://EXAMPLE.com/Path', policy) == 'https://example.com'
    assert check_target('https://example.com:443/', policy) == 'https://example.com'


def test_public_http_scheme_when_declared():
    policy = public_policy(schemes=('https', 'http'))
    assert check_target('http://example.com:80/x', policy) == 'http://example.com'


def test_public_entry_query_when_allowed():
    policy = public_policy(entry_url_query_allowed=True)
    assert check_target('https://example.com/?page=1', policy) == 'https://example.com'


def test_public_subdomain_matching_requires_flag():
    policy = public_policy(allowlist=[AllowRule(domain='example.com')])
    assert check_target('https://example.com/', policy) == 'https://example.com'
    with pytest.raises(TargetPolicyError) as excinfo:
        check_target('https://sub.example.com/', policy)
    assert str(excinfo.value) == 'target_not_in_allowlist'


# --- public_http 判定：拒绝路径（全部 fail-closed，带稳定码） ------------------

@pytest.mark.parametrize('url,code', [
    ('ftp://example.com/', 'public_scheme_not_allowed'),
    ('http://example.com/', 'public_scheme_not_allowed'),      # 默认仅 https
    ('https://example.com:8443/', 'unsupported_port'),
    ('https://user:pass@example.com/', 'invalid_url'),
    ('https://example.com/#frag', 'invalid_url'),
    ('https://example.com/?page=1', 'entry_url_query_not_supported'),
    ('https://127.0.0.1/', 'target_not_in_allowlist'),          # IPv4 字面量
    ('https://[::1]/', 'target_not_in_allowlist'),              # IPv6 字面量
    ('https://2130706433/', 'target_not_in_allowlist'),         # 十进制 IP 变体
    ('https://0x7f000001/', 'target_not_in_allowlist'),         # 十六进制 IP 变体
    ('https://notexample.com/', 'target_not_in_allowlist'),
    ('https://evilexample.com/', 'target_not_in_allowlist'),    # 点边界
    ('https://example.com.evil.net/', 'target_not_in_allowlist'),
    ('https://', 'invalid_url'),
    ('not a url', 'invalid_url'),
    ('https://example.com:abc/', 'invalid_url'),                # 非法端口
])
def test_public_rejections_carry_stable_codes(url, code):
    # 默认策略仅允许 https，http/ftp 均在 scheme 判定处拒绝。
    with pytest.raises(TargetPolicyError) as excinfo:
        check_target(url, public_policy())
    error = excinfo.value
    assert str(error) == code
    assert error.code == code
    assert code in NEW_CODES + REUSED_CODES
    # 任何拒绝都不可 repair、不可 retry（注册码走 SECURITY，新码 fail-closed）。
    classification = classify(error)
    assert classification.repairable is False and classification.retryable is False


def test_ip_literal_host_never_matches_permissive_rule():
    """即便管理员登记了形如 0.0.1 的宽后缀规则，IP 字面量主机仍被拒绝。"""
    policy = public_policy(allowlist=[AllowRule(domain='0.0.1', include_subdomains=True)])
    with pytest.raises(TargetPolicyError) as excinfo:
        check_target('https://127.0.0.1/', policy)
    assert str(excinfo.value) == 'target_not_in_allowlist'


# --- policy_sha256 -----------------------------------------------------------

def test_policy_sha256_is_stable_and_content_addressed():
    digest = policy_sha256(public_policy())
    assert re.fullmatch(r'[0-9a-f]{64}', digest)
    assert digest == policy_sha256(public_policy())
    assert digest != policy_sha256(public_policy(target_kind='browser'))
    assert digest != policy_sha256(public_policy(entry_url_query_allowed=True))
    assert digest != policy_sha256(public_policy(
        allowlist=[AllowRule(domain='example.com')]))           # include_subdomains 翻转
    assert digest != policy_sha256(default_policy())            # mode 不同


def test_policy_sha256_ignores_construction_order():
    a = TargetPolicy(mode='public_http', schemes=('https',),
                     allowlist=[AllowRule(domain='example.com')])
    b = TargetPolicy(allowlist=[AllowRule(domain='example.com')],
                     schemes=('https',), mode='public_http')
    assert policy_sha256(a) == policy_sha256(b)


# --- 冻结边界与分类行为 --------------------------------------------------------

def test_target_policy_error_contract():
    """TargetPolicyError 由 policy/errors.py 承载，不依赖冻结的 pipeline.errors 注册表。"""
    from backend.app.policy.errors import TargetPolicyError as Direct

    assert Direct is TargetPolicyError
    error = TargetPolicyError('target_not_in_allowlist')
    assert isinstance(error, ValueError)
    assert error.code == 'target_not_in_allowlist'
    assert str(error) == 'target_not_in_allowlist'


def test_target_module_does_not_depend_on_pipeline_errors():
    import backend.app.policy.errors as policy_errors
    import backend.app.policy.target as policy_target

    for module in (policy_errors, policy_target):
        assert not hasattr(module, 'PipelineError')


def test_new_codes_are_deferred_and_fail_closed():
    """S1 不改冻结的 errors.SPECS：新码未注册，classify 必须 fail-closed。

    S3 接入管线（里程碑批准解冻 backend）时，本测试随 SPECS 注册一起翻转：
    新码应迁移为 PipelineError 并断言 SECURITY/observing。
    """
    for code in NEW_CODES:
        assert code not in SPECS, code
        classification = classify(TargetPolicyError(code))
        assert classification.category is Category.UNCLASSIFIED, code
        assert classification.repairable is False and classification.retryable is False, code


def test_reused_registered_codes_classify_as_security():
    for code in REUSED_CODES:
        assert code in SPECS, code
        assert classify(TargetPolicyError(code)).category is Category.SECURITY, code


def test_legacy_loopback_code_spec_unchanged():
    spec = SPECS['only_literal_loopback_http_supported']
    assert spec.category is Category.SECURITY
    assert spec.repairable is False and spec.retryable is False
    assert spec.phase == 'executing'
    assert spec.detail_keys == frozenset()
    assert spec.summary == 'Target is not a literal loopback HTTP origin.'
