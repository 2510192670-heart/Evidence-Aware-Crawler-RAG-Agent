import asyncio
import json

import httpx
import pytest

from backend.app.pipeline.contracts import Observation, ExtractionPlan, pointer, cloud_summary, local_origin
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
