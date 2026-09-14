import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from backend.app.pipeline.contracts import (
    Observation,
    ExtractionPlan,
    has_sensitive_keys,
    pointer,
    cloud_summary,
    local_origin,
)
from backend.app.pipeline.errors import Category, PipelineError, classify
from backend.app.pipeline.execution import execute_plan


def record(query=None):
    return Observation(request_id='req_001', url='http://127.0.0.1:8000/api/catalog',
                       query=query or {'p': '1', 'size': '2'},
                       body={'total': 3, 'more': True, 'rows': [{'sku': 1, 'title': '商品', 'cost': 10}]})


def plan(**overrides):
    values = dict(request_id='req_001', items_pointer='/rows', fields={'id': '/sku', 'name': '/title'},
                  unique_key='id', page_parameter='p', has_next_pointer='/more', total_pointer='/total')
    values.update(overrides)
    return ExtractionPlan(**values)


def test_pointer_and_redaction():
    assert pointer({'a/b': {'~x': 2}}, '/a~1b/~0x') == 2
    with pytest.raises(ValueError):
        pointer({'a': 1}, 'a')
    summary = cloud_summary(Observation(request_id='x', url='http://127.0.0.1:8000/private',
        query={'p': '1', 'token': 'secret'}, body={'email': 'private@example.com', 'password': 'secret', 'rows': [{'name': 'Alice', 'id': 123}]}))
    text = json.dumps(summary)
    assert 'secret' not in text and 'Alice' not in text and 'private@example.com' not in text
    assert '127.0.0.1' not in text
    assert summary['response_shape']['rows'][0]['id'] == '<integer>'


@pytest.mark.parametrize('url', ['https://example.com', 'http://127.0.0.1.evil.test', 'http://user:pass@127.0.0.1', 'file:///a', 'http://localhost'])
def test_non_literal_loopback_is_rejected(url):
    with pytest.raises(ValueError):
        local_origin(url)


def test_complete_collection_and_partial_limit():
    def handler(request):
        p = int(request.url.params['p'])
        items = [{'sku': i, 'title': f'item{i}'} for i in ([1, 2] if p == 1 else [3])]
        return httpx.Response(200, json={'rows': items, 'total': 3, 'more': p == 1})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await execute_plan(client, plan(), [record()], max_pages=3)
            assert result['completeness'] == 'complete'
            assert result['items'] == [{'id': 1, 'name': 'item1'}, {'id': 2, 'name': 'item2'}, {'id': 3, 'name': 'item3'}]
            partial = await execute_plan(client, plan(), [record()], max_pages=1)
            assert partial['completeness'] == 'partial'
    asyncio.run(scenario())


@pytest.mark.parametrize('overrides,query', [({'request_id': 'invented'}, None), ({'page_parameter': 'invented'}, None), ({}, {'p': '1', 'token': 'secret'})])
def test_reject_plan_before_request(overrides, query):
    async def scenario():
        def handler(request):
            pytest.fail('Must reject before network request')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await execute_plan(client, plan(**overrides), [record(query)], 3)
    asyncio.run(scenario())


@pytest.mark.parametrize('body', [
    {'rows': [{'sku': 1, 'title': 'x'}, {'sku': 1, 'title': 'x'}], 'total': 2, 'more': False},
    {'rows': [], 'total': 3, 'more': True},
    {'rows': [{'sku': 1, 'title': 'x'}], 'total': 3, 'more': False},
    {'rows': [{'sku': 1}], 'total': 1, 'more': False},
])
def test_invalid_result_is_not_success(body):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
            with pytest.raises(ValueError):
                await execute_plan(client, plan(), [record()], 3)
    asyncio.run(scenario())


# --- M4.3.1 request-method evidence contract ----------------------------
#
# method/request_body 是证据属性：GET 的旧构造点必须保持不变，POST 必须携带一个
# 非空 JSON 对象请求体，任何其他组合都在契约层失败。

def post_record(**overrides):
    values = dict(request_id='req_001', url='http://127.0.0.1:8100/catalog/v1/query',
                  method='POST', query={},
                  request_body={'page': 1, 'page_size': 10},
                  body={'items': [{'sku': 1, 'title': 'x'}], 'total': 1, 'has_next': False})
    values.update(overrides)
    return Observation(**values)


def test_get_observation_defaults_stay_backward_compatible():
    legacy = record()
    assert legacy.method == 'GET'
    assert legacy.request_body is None


def test_post_observation_requires_a_non_empty_json_object_body():
    with pytest.raises(ValidationError):
        post_record(request_body=None)
    with pytest.raises(ValidationError):
        post_record(request_body={})
    with pytest.raises(ValidationError):
        post_record(request_body=[{'page': 1}])


def test_get_observation_must_not_carry_a_request_body():
    with pytest.raises(ValidationError):
        Observation(request_id='req_001', url='http://127.0.0.1:8000/api/catalog',
                    query={'p': '1'}, request_body={'page': 1}, body={'rows': []})


def test_unknown_method_is_rejected():
    with pytest.raises(ValidationError):
        post_record(method='PUT')


def test_sensitive_key_detection_is_recursive_and_bounded():
    assert has_sensitive_keys({'page': 1, 'nested': {'api_key': 'x'}})
    assert has_sensitive_keys({'rows': [{'token': 'x'}]})
    assert not has_sensitive_keys({'page': 1, 'page_size': 10, 'keyword': '样品'})


def test_post_summary_exposes_the_page_member_without_values():
    summary = cloud_summary(post_record(
        request_body={'page': 1, 'page_size': 10, 'keyword': '样品 01', 'token': 'secret'},
        body={'items': [{'sku': 1, 'title': '样品 01'}], 'total': 1, 'has_next': False}))
    assert summary['method'] == 'POST'
    # 页码/页大小可见，模型才能判断页码位于 JSON body；其余只留类型占位。
    assert summary['request_body_shape'] == {'page': 1, 'page_size': 10, 'keyword': '<string>'}
    text = json.dumps(summary, ensure_ascii=False)
    assert 'secret' not in text and '样品' not in text
    assert summary['response_shape']['items'][0]['title'] == '<string>'


def test_get_summary_keeps_the_original_shape():
    summary = cloud_summary(record())
    assert summary['method'] == 'GET'
    assert 'request_body_shape' not in summary
    assert summary['query'] == {'p': '1', 'size': '2'}


# --- M4.3.2 POST pagination: plan, shared validation, request shaping ---

def post_plan(**overrides):
    values = dict(request_id='req_001', items_pointer='/items',
                  fields={'sku': '/sku', 'title': '/title'}, unique_key='sku',
                  pagination_location='json_body', page_parameter='page',
                  has_next_pointer='/has_next', total_pointer='/total')
    values.update(overrides)
    return ExtractionPlan(**values)


def test_historical_plan_json_without_pagination_location_still_validates():
    legacy = ('{"request_id": "req_001", "items_pointer": "/rows",'
              ' "fields": {"id": "/sku", "name": "/title"}, "unique_key": "id",'
              ' "page_parameter": "p", "has_next_pointer": "/more", "total_pointer": "/total"}')
    plan = ExtractionPlan.model_validate_json(legacy)
    assert plan.pagination_location == 'query'
    assert plan.page_parameter == 'p'


def test_get_plan_defaults_to_query_location():
    assert plan().pagination_location == 'query'
    assert post_plan().pagination_location == 'json_body'


@pytest.mark.parametrize('location,make_record,expected_code', [
    ('json_body', record, 'page_location_mismatch'),
    ('query', lambda: post_record(request_body={'page': '1', 'page_size': 10}),
     'page_location_mismatch'),
    ('json_body', lambda: post_record(request_body={'page_size': 10}), 'unobserved_page_parameter'),
    ('json_body', lambda: post_record(request_body={'page': '1', 'page_size': 10}),
     'invalid_page_field_type'),
])
def test_pagination_location_is_validated_before_any_request(location, make_record, expected_code):
    async def scenario():
        def handler(request):
            pytest.fail('Must reject before network request')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError) as caught:
                await execute_plan(client, post_plan(pagination_location=location), [make_record()], 3)
            return caught.value

    error = asyncio.run(scenario())
    assert str(error) == expected_code
    if expected_code != 'unobserved_page_parameter':
        # 新码按 M4.2 契约携带分类；unobserved_page_parameter 尚未迁移（保持原样）。
        assert isinstance(error, PipelineError)
        assert classify(error).category is Category.PLAN_SEMANTIC
        assert error.repairable is True and error.retryable is False


def test_post_pagination_changes_only_the_page_member():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append({'method': request.method, 'body': payload, 'params': dict(request.url.params),
                     'content_type': request.headers.get('content-type', '')})
        page = payload['page']
        return httpx.Response(200, json={'items': [{'sku': page, 'title': f'item{page}'}],
                                         'total': 2, 'has_next': page < 2})

    observation = post_record(request_body={'page': 1, 'page_size': 10, 'filters': {'active': True}},
                              query={'lang': 'zh'})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execute_plan(client, post_plan(), [observation], 3)

    result = asyncio.run(scenario())
    assert result['completeness'] == 'complete'
    assert [entry['method'] for entry in seen] == ['POST', 'POST']
    assert [entry['body']['page'] for entry in seen] == [1, 2]
    # 只有 page 变化：其余成员（含 page_size 与嵌套结构）逐字段相同。
    assert seen[0]['body'] == {**seen[1]['body'], 'page': 1}
    assert seen[0]['body']['page_size'] == seen[1]['body']['page_size'] == 10
    assert seen[0]['body']['filters'] == seen[1]['body']['filters'] == {'active': True}
    # 观察到的 query 原样回放，且不参与翻页。
    assert seen[0]['params'] == seen[1]['params'] == {'lang': 'zh'}
    assert all(entry['content_type'].startswith('application/json') for entry in seen)


def test_post_pagination_reports_partial_when_the_page_budget_runs_out():
    def handler(request):
        page = json.loads(request.content)['page']
        return httpx.Response(200, json={'items': [{'sku': page, 'title': 'x'}], 'total': 5,
                                         'has_next': page < 5})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execute_plan(client, post_plan(), [post_record()], 2)

    result = asyncio.run(scenario())
    assert result['completeness'] == 'partial'
    assert result['pages'] == 2
    assert [item['sku'] for item in result['items']] == [1, 2]


def test_get_query_pagination_still_works_unchanged():
    # 反向组合：GET 记录 + query 位置仍是唯一被允许的 GET 组合，行为不变。
    async def scenario():
        def handler(request):
            return httpx.Response(200, json={'rows': [{'sku': 1, 'title': 'x'}], 'total': 1, 'more': False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execute_plan(client, plan(), [record()], 2)

    assert asyncio.run(scenario())['completeness'] == 'complete'


