"""M7-A S2: TargetPolicy 等价性快照基线测试。

目标：证明 S1 policy 层不改变原 loopback 行为，并钉住 public_http 基础策略判定。

三组覆盖：
A. loopback 等价性——快照中每条 accepted/rejected 都同时跑 check_target 与
   local_origin，二者结果/异常类型/错误码必须逐一相同（委托是逐字节的，
   拒绝路径保持裸 ValueError，不升级为 TargetPolicyError）；
B. public_http 基础策略——允许 https://example.com，拒绝 file:// / ftp:// /
   私网与回环 IP 字面量；
C. 错误契约——TargetPolicyError 的 code 属性、str(error)、ValueError 子类关系。

快照 tests/snapshots/m7_policy/loopback_behavior.json 是行为基线：本测试只读它，
任何未来切片（S3 接入管线）若改变判定行为，都会在这里显式失败而不是静默漂移。
完全离线：不发网络请求、不做 DNS 解析、不触碰 Playwright、不修改任何生产文件。
"""

import json
from pathlib import Path

import pytest

from backend.app.pipeline.contracts import local_origin
from backend.app.policy import TargetPolicy, TargetPolicyError, check_target

SNAPSHOT_PATH = Path(__file__).resolve().parent / 'snapshots' / 'm7_policy' / 'loopback_behavior.json'
# 静态基线文件，模块级加载一次；参数化直接遍历条目，快照增长不会静默漏测。
SNAPSHOT = json.loads(SNAPSHOT_PATH.read_text(encoding='utf-8'))

LOOPBACK_POLICY = TargetPolicy(**SNAPSHOT['loopback']['policy'])
PUBLIC_POLICY = TargetPolicy(**SNAPSHOT['public_http']['policy'])


def entry_id(entry):
    return entry['url']


# --- 快照形态 -------------------------------------------------------------

def test_snapshot_is_well_formed():
    assert SNAPSHOT['schema_version'] == 1
    assert SNAPSHOT['milestone'] == 'm7-a-s2'
    assert set(SNAPSHOT) == {'schema_version', 'milestone', 'generated_from',
                             'description', 'loopback', 'public_http'}
    assert SNAPSHOT['loopback']['policy'] == {'mode': 'loopback'}
    for entry in SNAPSHOT['loopback']['accepted'] + SNAPSHOT['public_http']['accepted']:
        assert set(entry) == {'url', 'origin'}
    for entry in SNAPSHOT['loopback']['rejected']:
        assert set(entry) == {'url', 'error'}
    for entry in SNAPSHOT['public_http']['rejected']:
        assert set(entry) == {'url', 'code'}


def test_snapshot_covers_required_families():
    """覆盖度守卫：localhost 与 127.0.0.1（A）、file/ftp/私网 IP（B）必须在基线内。"""
    loopback_urls = [entry['url'] for entry in
                     SNAPSHOT['loopback']['accepted'] + SNAPSHOT['loopback']['rejected']]
    assert any('localhost' in url for url in loopback_urls)
    assert any(url.startswith('http://127.0.0.1') for url in loopback_urls)
    public_urls = [entry['url'] for entry in SNAPSHOT['public_http']['rejected']]
    assert any(url.startswith('file://') for url in public_urls)
    assert any(url.startswith('ftp://') for url in public_urls)
    for private in ('https://127.0.0.1/', 'https://[::1]/',
                    'https://192.168.1.1/', 'https://10.0.0.5/'):
        assert private in public_urls, private


# --- A. loopback 等价性：check_target ≡ local_origin ------------------------

@pytest.mark.parametrize('entry', SNAPSHOT['loopback']['accepted'], ids=entry_id)
def test_loopback_accepted_equivalent_to_local_origin(entry):
    url, origin = entry['url'], entry['origin']
    assert check_target(url, LOOPBACK_POLICY) == origin
    assert local_origin(url) == origin
    # policy=None 的默认路径同样等价（既有调用方零漂移）。
    assert check_target(url) == origin


@pytest.mark.parametrize('entry', SNAPSHOT['loopback']['rejected'], ids=entry_id)
def test_loopback_rejected_equivalent_to_local_origin(entry):
    url, code = entry['url'], entry['error']
    with pytest.raises(ValueError) as via_policy:
        check_target(url, LOOPBACK_POLICY)
    with pytest.raises(ValueError) as via_legacy:
        local_origin(url)
    # 委托必须保留裸 ValueError：异常类型精确一致，不得升级为 TargetPolicyError。
    assert type(via_policy.value) is ValueError
    assert type(via_legacy.value) is ValueError
    assert str(via_policy.value) == code
    assert str(via_legacy.value) == code


def test_loopback_rejection_is_not_target_policy_error():
    """loopback 拒绝绝不进入 policy 错误通道（等价性的反向断言）。"""
    with pytest.raises(ValueError) as excinfo:
        check_target('http://localhost:8000/', LOOPBACK_POLICY)
    assert not isinstance(excinfo.value, TargetPolicyError)


# --- B. public_http 基础策略 -------------------------------------------------

@pytest.mark.parametrize('entry', SNAPSHOT['public_http']['accepted'], ids=entry_id)
def test_public_accepted(entry):
    assert check_target(entry['url'], PUBLIC_POLICY) == entry['origin']


@pytest.mark.parametrize('entry', SNAPSHOT['public_http']['rejected'], ids=entry_id)
def test_public_rejected(entry):
    with pytest.raises(TargetPolicyError) as excinfo:
        check_target(entry['url'], PUBLIC_POLICY)
    assert excinfo.value.code == entry['code']
    assert str(excinfo.value) == entry['code']


def test_public_policy_shape():
    assert PUBLIC_POLICY.mode == 'public_http'
    assert PUBLIC_POLICY.schemes == ('https',)
    assert [rule.domain for rule in PUBLIC_POLICY.allowlist] == ['example.com']


# --- C. 错误契约 --------------------------------------------------------------

def test_target_policy_error_contract():
    error = TargetPolicyError('target_not_in_allowlist')
    assert isinstance(error, ValueError)
    assert error.code == 'target_not_in_allowlist'
    assert str(error) == 'target_not_in_allowlist'
