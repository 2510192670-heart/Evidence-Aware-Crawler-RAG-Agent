"""M7-A S3.2-A: TargetPolicy enforcement 层测试。

四组覆盖：
A. default loopback policy——允许字面量 127.0.0.1/::1，拒绝公网；
   localhost 按 S2 快照基线维持拒绝（单一真源委托 local_origin，
   enforcement 层不得引入第二套判定，也不得放宽冻结语义）；
B. public_http allow——allowlist 域命中（含点边界子域）；
C. public_http deny——未命中域 / 非 https / IP 字面量 / 非默认端口 → TargetPolicyError；
D. malformed url——两种 mode 下的稳定拒绝码。

完全离线；断言 enforcement.check_target 与 policy.target.check_target、
local_origin 三者在 loopback 下逐一等价（不存在平行判定）。
"""

import pytest

from backend.app.pipeline.contracts import local_origin
from backend.app.policy import AllowRule, TargetPolicy, TargetPolicyError
from backend.app.policy import enforcement
from backend.app.policy.enforcement import check_target


def public_policy(**overrides) -> TargetPolicy:
    base = dict(mode='public_http',
                allowlist=[AllowRule(domain='example.com', include_subdomains=True)])
    base.update(overrides)
    return TargetPolicy(**base)


# --- A. default loopback policy ------------------------------------------------

@pytest.mark.parametrize('url,origin', [
    ('http://127.0.0.1:8000/', 'http://127.0.0.1:8000'),
    ('http://127.0.0.1:8123/a/b?page=2', 'http://127.0.0.1:8123'),
    ('http://[::1]:9000/', 'http://[::1]:9000'),
])
def test_loopback_allows_literal_loopback(url, origin):
    assert check_target(url) == origin                              # policy=None 默认
    assert check_target(url, TargetPolicy()) == origin              # 显式 loopback
    assert check_target(url) == local_origin(url)                   # 单一真源等价


@pytest.mark.parametrize('url', [
    'https://example.com/',          # 公网
    'http://example.com/x',          # 公网 http
    'https://93.184.216.34/',        # 公网 IP 字面量
])
def test_loopback_rejects_public_targets(url):
    with pytest.raises(ValueError) as excinfo:
        check_target(url)
    assert type(excinfo.value) is ValueError                        # 保持裸 ValueError
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'
    assert not isinstance(excinfo.value, TargetPolicyError)


def test_loopback_localhost_stays_rejected_per_snapshot():
    """localhost 维持拒绝：S2 快照基线 + local_origin 字面量 loopback 安全模型。

    enforcement 层若在此放宽，就制造了第二套 loopback 判定并在 S3.2-B 接线时
    改变 API 既有 422 行为——两者都被"保持 S1/S2/S3.1 行为不变"约束禁止。
    """
    with pytest.raises(ValueError) as excinfo:
        check_target('http://localhost:8000/')
    assert type(excinfo.value) is ValueError
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'
    # 与 target 层、local_origin 三方一致（无平行判定）。
    from backend.app.policy.target import check_target as target_check
    for call in (lambda: target_check('http://localhost:8000/'),
                 lambda: local_origin('http://localhost:8000/')):
        with pytest.raises(ValueError) as reference:
            call()
        assert str(reference.value) == str(excinfo.value)


def test_enforcement_exports_same_error_type():
    assert enforcement.TargetPolicyError is TargetPolicyError


# --- B. public_http allow --------------------------------------------------------

def test_public_allows_allowlisted_domain():
    policy = public_policy()
    assert check_target('https://example.com', policy) == 'https://example.com'
    assert check_target('https://example.com/items/list', policy) == 'https://example.com'
    assert check_target('https://example.com:443/', policy) == 'https://example.com'


def test_public_allows_subdomain_only_with_flag():
    flagged = public_policy()
    assert check_target('https://sub.example.com/', flagged) == 'https://sub.example.com'
    strict = public_policy(allowlist=[AllowRule(domain='example.com')])
    assert check_target('https://example.com/', strict) == 'https://example.com'
    with pytest.raises(TargetPolicyError) as excinfo:
        check_target('https://sub.example.com/', strict)
    assert excinfo.value.code == 'target_not_in_allowlist'


# --- C. public_http deny -----------------------------------------------------------

@pytest.mark.parametrize('url,code', [
    ('https://notexample.com/', 'target_not_in_allowlist'),
    ('https://evilexample.com/', 'target_not_in_allowlist'),        # 点边界
    ('https://example.com.evil.net/', 'target_not_in_allowlist'),
    ('https://93.184.216.34/', 'target_not_in_allowlist'),          # 公网 IP 字面量
    ('https://10.0.0.1/', 'target_not_in_allowlist'),               # 内网 IP
    ('https://127.0.0.1/', 'target_not_in_allowlist'),              # 回环 IP
    ('https://[::1]/', 'target_not_in_allowlist'),
    ('http://example.com/', 'public_scheme_not_allowed'),           # 默认仅 https
    ('ftp://example.com/', 'public_scheme_not_allowed'),
    ('https://example.com:8443/', 'unsupported_port'),
])
def test_public_denials_carry_stable_codes(url, code):
    with pytest.raises(TargetPolicyError) as excinfo:
        check_target(url, public_policy())
    error = excinfo.value
    assert isinstance(error, ValueError)
    assert error.code == code
    assert str(error) == code


# --- D. malformed url ----------------------------------------------------------------

@pytest.mark.parametrize('url', ['', 'not a url', 'https://', 'https://example.com:abc/',
                                 'https://user:pass@example.com/', 'https://example.com/#frag'])
def test_public_malformed_urls_rejected_as_invalid_url(url):
    with pytest.raises(TargetPolicyError) as excinfo:
        check_target(url, public_policy())
    assert excinfo.value.code == 'invalid_url'


@pytest.mark.parametrize('url', ['', 'not a url', '://broken'])
def test_loopback_malformed_urls_keep_legacy_code(url):
    """loopback 下畸形 URL 仍走 local_origin 的既有拒绝码（不引入新码）。"""
    with pytest.raises(ValueError) as excinfo:
        check_target(url)
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'
    assert local_origin_raises_same(url)


def local_origin_raises_same(url) -> bool:
    try:
        local_origin(url)
    except ValueError as error:
        return str(error) == 'only_literal_loopback_http_supported'
    return False
