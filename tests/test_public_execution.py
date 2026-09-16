"""M7-A S3.3-C3-A: 执行能力层测试（validate_plan 注入点 + execute_plan policy 参数）。

四组覆盖：
A. policy=None 与旧 loopback 路径完全等价（含异常类型与错误码逐字节一致）；
B. target_check 注入语义（默认=local_origin；注入 callable 被精确调用一次）；
C. 非 allowlist record 在发出任何请求之前 BLOCK（零网络副作用）；
D. public policy + MockTransport 成功路径（GET query 与 POST json_body 两种分页，
   逐页仅 page 成员变化）。

完全离线：httpx.MockTransport，不创建公网真实请求，不调用云模型，
不触达 run.py 守卫（public_mode_not_enabled 保持原样）。
"""

import asyncio
import json

import httpx
import pytest

from backend.app.pipeline.contracts import ExtractionPlan, Observation, validate_plan
from backend.app.pipeline.execution import execute_plan
from backend.app.policy import (AllowRule, TargetPolicy, TargetPolicyError,
                                check_target, default_policy)

PUBLIC_POLICY = TargetPolicy(mode='public_http',
                             allowlist=[AllowRule(domain='example.com', include_subdomains=True)])

PAGE_1 = {'items': [{'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}], 'total': 4}
PAGE_2 = {'items': [{'id': 3, 'name': 'c'}, {'id': 4, 'name': 'd'}], 'total': 4}


def get_plan(**overrides):
    base = dict(request_id='req_001', items_pointer='/items',
                fields={'id': '/id', 'name': '/name'}, unique_key='id',
                has_next_pointer=None, page_parameter='page', total_pointer='/total')
    base.update(overrides)
    return ExtractionPlan(**base)


def loopback_record():
    return Observation(request_id='req_001', url='http://127.0.0.1:8000/api',
                       query={'page': '1'}, body=dict(PAGE_1))


def public_record(url='https://example.com/api'):
    return Observation(request_id='req_001', url=url,
                       query={'page': '1'}, body=dict(PAGE_1))


def public_post_record():
    return Observation(request_id='req_001', url='https://example.com/api',
                       method='POST', query={}, request_body={'page': 1, 'q': 'widget'},
                       body=dict(PAGE_1))


def paged_handler(requests_log):
    """按 page 参数返回两页数据；记录每个请求供语义比对。"""
    def handler(request):
        requests_log.append(request)
        if request.method == 'POST':
            page = json.loads(request.read())['page']
        else:
            page = int(request.url.params['page'])
        return httpx.Response(200, json=PAGE_1 if page == 1 else PAGE_2)
    return handler


def run_execute(handler, plan, records, max_pages=2, policy=None):
    """离线执行：MockTransport 客户端 + execute_plan，异常原样透出。"""
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     trust_env=False) as client:
            return await execute_plan(client, plan, records, max_pages, policy=policy)
    return asyncio.run(go())


# --- A. policy=None 与旧 loopback 路径等价 ---------------------------------------

def test_validate_plan_default_rejects_public_record_with_legacy_error():
    with pytest.raises(ValueError) as excinfo:
        validate_plan(get_plan(), public_record())
    assert type(excinfo.value) is ValueError          # 裸 ValueError，不升级
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'


def test_execute_plan_policy_none_equals_loopback_policy():
    """policy=None 与显式 loopback 策略产出逐键相同的结果（等价性）。"""
    plan = get_plan()
    log_none, log_loopback = [], []
    result_none = run_execute(paged_handler(log_none), plan, [loopback_record()])
    result_loopback = run_execute(paged_handler(log_loopback), plan, [loopback_record()],
                                  policy=default_policy())
    assert result_none == result_loopback
    assert result_none['completeness'] == 'complete'
    assert result_none['pages'] == 2 and len(result_none['items']) == 4
    assert [str(r.url) for r in log_none] == [str(r.url) for r in log_loopback]


def test_execute_plan_policy_none_rejects_public_record():
    log = []
    with pytest.raises(ValueError) as excinfo:
        run_execute(paged_handler(log), get_plan(), [public_record()], policy=None)
    assert type(excinfo.value) is ValueError
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'
    assert log == []                                   # 拒绝先于任何请求


# --- B. target_check 注入语义 -------------------------------------------------------

def test_validate_plan_target_check_injection():
    calls = []

    def spy(url):
        calls.append(url)
        return check_target(url, PUBLIC_POLICY)

    sample_types = validate_plan(get_plan(), public_record(), target_check=spy)
    assert calls == ['https://example.com/api']        # 恰好一次，参数为 record.url
    assert sample_types == {'id': int, 'name': str}


def test_validate_plan_loopback_record_with_public_target_check_blocked():
    """注入公网判定后，loopback record 反向被拒（判定完全由注入方决定）。

    拒绝码遵循单一真源判定顺序 scheme → 端口 → query → 主机：http 记录先命中
    public_scheme_not_allowed。
    """
    with pytest.raises(TargetPolicyError) as excinfo:
        validate_plan(get_plan(), loopback_record(),
                      target_check=lambda url: check_target(url, PUBLIC_POLICY))
    assert excinfo.value.code == 'public_scheme_not_allowed'


# --- C. 非 allowlist record BLOCK（零网络副作用） ------------------------------------

@pytest.mark.parametrize('url,code', [
    ('https://evil.com/api', 'target_not_in_allowlist'),
    ('https://example.com.evil.net/api', 'target_not_in_allowlist'),
    ('http://example.com/api', 'public_scheme_not_allowed'),
    ('https://10.0.0.1/api', 'target_not_in_allowlist'),
])
def test_execute_plan_blocks_non_allowlisted_record_before_any_request(url, code):
    log = []
    with pytest.raises(TargetPolicyError) as excinfo:
        run_execute(paged_handler(log), get_plan(), [public_record(url)],
                    policy=PUBLIC_POLICY)
    assert excinfo.value.code == code
    assert log == []


# --- D. public policy + MockTransport 成功路径 ---------------------------------------

def test_public_get_pagination_only_page_parameter_changes():
    log = []
    result = run_execute(paged_handler(log), get_plan(), [public_record()],
                         policy=PUBLIC_POLICY)
    assert result['pages'] == 2
    assert result['expected_total'] == 4
    assert result['completeness'] == 'complete'
    assert [item['id'] for item in result['items']] == [1, 2, 3, 4]
    # 逐页仅 page 参数变化：host/path/其余成员与观察证据一致。
    assert [str(r.url) for r in log] == ['https://example.com/api?page=1',
                                         'https://example.com/api?page=2']
    assert all(r.method == 'GET' for r in log)


def test_public_post_json_body_pagination_only_page_member_changes():
    log = []
    plan = get_plan(pagination_location='json_body')
    result = run_execute(paged_handler(log), plan, [public_post_record()],
                         policy=PUBLIC_POLICY)
    assert result['completeness'] == 'complete' and result['pages'] == 2
    bodies = [json.loads(r.read()) for r in log]
    # 只改 body 顶层页码成员，其余成员（q）原样回放。
    assert bodies == [{'page': 1, 'q': 'widget'}, {'page': 2, 'q': 'widget'}]
    assert all(str(r.url) == 'https://example.com/api' for r in log)
    assert all(r.method == 'POST' for r in log)


def test_public_data_integrity_checks_still_apply():
    """公网路径的 DATA_INTEGRITY 校验与 loopback 完全同套（无第二套判定）。"""
    def drifting_handler(request):
        page = int(request.url.params['page'])
        if page == 1:
            return httpx.Response(200, json=PAGE_1)
        return httpx.Response(200, json={'items': [{'id': 1, 'name': 'x'},   # 重复 id
                                                   {'id': 4, 'name': 'd'}],
                                         'total': 4})
    with pytest.raises(ValueError) as excinfo:          # PipelineError 基类
        run_execute(drifting_handler, get_plan(), [public_record()], policy=PUBLIC_POLICY)
    assert str(excinfo.value) == 'duplicate_id'
