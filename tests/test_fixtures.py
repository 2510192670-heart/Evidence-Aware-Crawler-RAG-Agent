"""Fixture behaviour and current-pipeline compatibility for the M4.1 targets.

These tests are the answer-key verification: they prove the deterministic
fixtures behave exactly as ``benchmarks/tasks.json`` declares, and that the
production pipeline can still only do what M3 allowed (GET page-number).
"""

import asyncio
import json
import math
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.pipeline.contracts import ExtractionPlan, Observation, cloud_summary, pointer
from backend.app.pipeline.curl_import import parse_curl
from backend.app.pipeline.errors import Category, PipelineError
from backend.app.pipeline.execution import execute_plan
from backend.app.pipeline.observe import MAX_BODY_BYTES, observe, request_json_object
from benchmarks import load as load_manifest
from fixtures.app import app
from fixtures.pages import PAGE_SPECS

client = TestClient(app)

TASKS = load_manifest()['tasks']
DRIFT_TASKS = [task for task in TASKS if task['scenario'] == 'S5']
GET_TASKS = [task for task in TASKS if task['solution']['method'] == 'GET']

# Distractors are grouped by cluster: (path, reuses the real container key).
DISTRACTORS = {
    's4-dev-1': [('/catalog/v1/items-meta', False), ('/catalog/v1/status', False)],
    's4-dev-2': [('/catalog/v1/search-facets', False), ('/catalog/v1/health', False)],
    's4-held-1': [('/inventory/v2/lines-preview', True), ('/inventory/v2/meta', False)],
    's4-held-2': [('/inventory/v2/ledger-summary', True), ('/inventory/v2/probe', False)],
}


@pytest.fixture(scope='module')
def fixture_server():
    """Serve the fixture app on a free loopback port for real HTTP execution."""
    import uvicorn
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail('fixture server did not start')
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True
    thread.join(timeout=10)


def fetch_page(base_url, task, page):
    solution = task['solution']
    params = {solution['pagination_parameter']: page, solution['page_size_parameter']: task['page_size']}
    if solution['method'] == 'POST':
        response = httpx.post(base_url + solution['request_path'], json=params, timeout=10, trust_env=False)
    else:
        response = httpx.get(base_url + solution['request_path'], params=params, timeout=10, trust_env=False)
    response.raise_for_status()
    return response.json()


def observation_for(base_url, task):
    solution = task['solution']
    return Observation(request_id='req_001', url=base_url + solution['request_path'],
                       query={solution['pagination_parameter']: '1',
                              solution['page_size_parameter']: str(task['page_size'])},
                       body=fetch_page(base_url, task, 1))


def plan_for(task):
    solution = task['solution']
    return ExtractionPlan(
        request_id='req_001',
        items_pointer=solution['items_pointer'],
        fields={name: '/' + name for name in task['fields']},
        unique_key=solution['unique_key'],
        page_parameter=solution['pagination_parameter'],
        has_next_pointer=solution['has_next_pointer'],
        total_pointer=solution['total_pointer'])


# --- existing learning site stays the regression baseline ---------------

def test_fixture_app_is_separate_from_the_production_site():
    # New structures live in fixtures/, so the old site must not grow them.
    assert client.get('/api/products').status_code == 404
    from main import app as production_app
    body = TestClient(production_app).get('/api/products', params={'page': 1, 'page_size': 10}).json()
    assert body['total'] == 30
    assert len(body['items']) == 10
    assert body['has_next'] is True


# --- fixture ground truth ----------------------------------------------

@pytest.mark.parametrize('scenario', ['S1', 'S2', 'S3', 'S4'])
def test_declared_counts_match_fixture_totals(fixture_server, scenario):
    for task in (t for t in TASKS if t['scenario'] == scenario):
        expected = task['expected']
        body = fetch_page(fixture_server, task, 1)
        total = pointer(body, task['solution']['total_pointer'])
        assert total == expected['record_count'], task['id']
        assert math.ceil(total / task['page_size']) == expected['pages'], task['id']


@pytest.mark.parametrize('task', DRIFT_TASKS, ids=[t['id'] for t in DRIFT_TASKS])
def test_drift_fixtures_fail_with_the_declared_code(fixture_server, task):
    record = observation_for(fixture_server, task)

    async def scenario():
        async with httpx.AsyncClient(trust_env=False) as http_client:
            with pytest.raises(ValueError) as caught:
                await execute_plan(http_client, plan_for(task), [record], task['max_pages'])
            return caught.value

    error = asyncio.run(scenario())
    assert str(error) == task['expected']['failure_kind']
    # M4.2：同样的字符串，现在带确定性分类。
    assert isinstance(error, PipelineError)
    assert error.category is Category.DATA_INTEGRITY
    assert error.repairable is False


def test_fixture_responses_are_repeatable(fixture_server):
    for task in GET_TASKS:
        assert fetch_page(fixture_server, task, 1) == fetch_page(fixture_server, task, 1), task['id']


@pytest.mark.parametrize('task', GET_TASKS, ids=[t['id'] for t in GET_TASKS])
def test_fixture_rejects_invalid_page_number(fixture_server, task):
    solution = task['solution']
    response = httpx.get(fixture_server + solution['request_path'],
                         params={solution['pagination_parameter']: 0}, timeout=10, trust_env=False)
    assert response.status_code == 422


# --- current pipeline vs. new structures -------------------------------

@pytest.mark.parametrize('scenario', ['S1', 'S3', 'S4'])
def test_executor_collects_supported_fixtures(fixture_server, scenario):
    """S1/S3/S4 must work with the unchanged M3 executor."""
    for task in (t for t in TASKS if t['scenario'] == scenario):
        expected = task['expected']
        record = observation_for(fixture_server, task)

        async def scenario_run():
            async with httpx.AsyncClient(trust_env=False) as http_client:
                return await execute_plan(http_client, plan_for(task), [record], task['max_pages'])

        result = asyncio.run(scenario_run())
        assert result['completeness'] == expected['completeness'], task['id']
        assert len(result['items']) == expected['record_count'], task['id']
        assert result['pages'] == expected['pages'], task['id']
        keys = [item[task['solution']['unique_key']] for item in result['items']]
        assert len(set(keys)) == len(keys), task['id']
        assert set(result['items'][0]) == set(task['fields']), task['id']


@pytest.mark.parametrize('task_id', sorted(DISTRACTORS))
def test_distractors_cannot_satisfy_the_validated_plan(fixture_server, task_id):
    task = next(t for t in TASKS if t['id'] == task_id)
    solution = task['solution']
    for path, mimics_container in DISTRACTORS[task_id]:
        body = httpx.get(fixture_server + path, timeout=10, trust_env=False).json()
        # Without a resolvable total the executor rejects the plan before any request.
        with pytest.raises(ValueError):
            pointer(body, solution['total_pointer'])
        if mimics_container:
            assert isinstance(pointer(body, solution['items_pointer']), list)


def test_post_fixtures_are_served_and_the_contract_stays_strict(fixture_server):
    body = httpx.post(fixture_server + '/catalog/v1/query',
                      json={'page': 1, 'page_size': 10}, timeout=10, trust_env=False).json()
    assert body['total'] == 20 and len(body['items']) == 10

    # POST 证据必须携带请求体：只声明方法不足以构成可复用证据。
    with pytest.raises(ValidationError):
        Observation(request_id='r', url='http://127.0.0.1:8100/catalog/v1/query',
                    query={'page': '1'}, body=body, method='POST')
    # 计划字段严格：分页位置必须用 pagination_location，其他字段名一律拒绝。
    with pytest.raises(ValidationError):
        ExtractionPlan(request_id='r', items_pointer='/items', fields={'sku': '/sku'}, unique_key='sku',
                       page_parameter='page', has_next_pointer='/has_next', total_pointer='/total',
                       location='json_body')
    # cURL 导入已支持 POST JSON（M4.3.3）：仍缺少 JSON 请求体的 POST 会被拒绝，
    # 携带 JSON 对象请求体并声明 application/json 的 POST 才可执行。
    assert parse_curl('curl http://127.0.0.1:8100/catalog/v1/query -X POST')['executable'] is False
    assert parse_curl("curl http://127.0.0.1:8100/catalog/v1/query -X POST "
                      "-H 'Content-Type: application/json' "
                      "--data '{\"page\": 1, \"page_size\": 10}'")['executable'] is True


def test_executor_only_issues_get_requests():
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={'items': [{'id': 1, 'name': 'x'}], 'total': 1, 'has_next': False})

    record = Observation(request_id='req_001', url='http://127.0.0.1:8100/catalog/v1/items',
                         query={'page': '1', 'per_page': '1'},
                         body={'items': [{'id': 1, 'name': 'x'}], 'total': 1, 'has_next': False})
    plan = ExtractionPlan(request_id='req_001', items_pointer='/items',
                          fields={'id': '/id', 'name': '/name'}, unique_key='id',
                          page_parameter='page', has_next_pointer='/has_next', total_pointer='/total')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await execute_plan(http_client, plan, [record], 3)

    result = asyncio.run(scenario())
    assert methods == ['GET']
    assert result['completeness'] == 'complete'


# --- M4.3.2 POST JSON page-number execution ----------------------------
#
# 与 S1/S3/S4 使用同一份答案键：完整性逻辑不变，只有请求构造（POST JSON body）
# 与页码位置（body 顶层成员）不同。

POST_TASKS = [task for task in TASKS if task['solution']['method'] == 'POST']


def post_plan_for(task):
    solution = task['solution']
    return ExtractionPlan(
        request_id='req_001',
        items_pointer=solution['items_pointer'],
        fields={name: '/' + name for name in task['fields']},
        unique_key=solution['unique_key'],
        pagination_location='json_body',
        page_parameter=solution['pagination_parameter'],
        has_next_pointer=solution['has_next_pointer'],
        total_pointer=solution['total_pointer'])


def post_observation_for(base_url, task):
    solution = task['solution']
    request_body = {solution['pagination_parameter']: 1,
                    solution['page_size_parameter']: task['page_size']}
    return Observation(request_id='req_001', url=base_url + solution['request_path'],
                       method='POST', query={}, request_body=request_body,
                       body=fetch_page(base_url, task, 1))


@pytest.mark.parametrize('task', POST_TASKS, ids=[t['id'] for t in POST_TASKS])
def test_executor_collects_post_fixtures(fixture_server, task):
    """S2 四个 POST 任务必须由同一个执行器真实采集成完整结果。"""
    expected = task['expected']
    record = post_observation_for(fixture_server, task)

    async def scenario():
        async with httpx.AsyncClient(trust_env=False) as http_client:
            return await execute_plan(http_client, post_plan_for(task), [record], task['max_pages'])

    result = asyncio.run(scenario())
    assert result['completeness'] == expected['completeness'], task['id']
    assert len(result['items']) == expected['record_count'], task['id']
    assert result['pages'] == expected['pages'], task['id']
    keys = [item[task['solution']['unique_key']] for item in result['items']]
    assert len(set(keys)) == len(keys), task['id']
    assert set(result['items'][0]) == set(task['fields']), task['id']


def test_executor_issues_post_and_keeps_the_page_size_verbatim(fixture_server):
    """请求捕获：方法恒为 POST，只有 page 变化，page_size 与其他成员保持不变。"""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.method, body, dict(request.url.params)))
        page = body['page']
        return httpx.Response(200, json={'items': [{'sku': page, 'title': 'x', 'amount_fen': 100}],
                                         'total': 3, 'has_next': page < 3})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await execute_plan(http_client, post_plan_for(POST_TASKS[0]),
                                      [post_observation_for(fixture_server, POST_TASKS[0])], 3)

    result = asyncio.run(scenario())
    assert result['completeness'] == 'complete'
    assert [(method, body['page']) for method, body, _ in seen] == [
        ('POST', 1), ('POST', 2), ('POST', 3)]
    stable = {key: value for key, value in seen[0][1].items() if key != 'page'}
    for _, body, _ in seen:
        assert {key: value for key, value in body.items() if key != 'page'} == stable
    assert stable['page_size'] == POST_TASKS[0]['page_size']
    assert all(params == {} for _, _, params in seen)


# --- entry pages --------------------------------------------------------

def test_every_manifest_entry_url_has_a_fixture_page():
    paths = {urlsplit(task['entry_url']).path for task in TASKS}
    assert paths == {'/pages/' + name for name in PAGE_SPECS}
    for path in sorted(paths):
        response = client.get(path)
        assert response.status_code == 200, path
        assert '下一页' in response.text
        assert 'fetch(' in response.text


def test_unknown_fixture_page_is_not_served():
    assert client.get('/pages/not-a-fixture').status_code == 404


# --- M4.3.1 POST JSON evidence ------------------------------------------
#
# 观察边界从「同源 GET」放宽到「同源 GET + 同源 JSON POST」。这些测试同时证明
# 放宽没有溢出：其他方法、表单请求体与敏感请求体都不能成为证据。

def serve_app(app_object):
    """在回环端口以线程运行一个测试专用 ASGI 应用，返回 (base_url, stop)。"""
    import uvicorn
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app_object, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail('loopback test server did not start')

    def stop():
        server.should_exit = True
        thread.join(timeout=10)
    return f'http://127.0.0.1:{port}', stop


def test_request_json_object_accepts_only_bounded_json_objects():
    json_headers = {'content-type': 'application/json; charset=utf-8'}
    assert request_json_object(json_headers, '{"page": 1}') == {'page': 1}
    assert request_json_object({'Content-Type': 'application/json'}, '{"page": 1}') == {'page': 1}
    assert request_json_object({'content-type': 'application/x-www-form-urlencoded'}, 'page=1') is None
    assert request_json_object(json_headers, None) is None
    assert request_json_object(json_headers, '[]') is None
    assert request_json_object(json_headers, '{}') is None
    assert request_json_object(json_headers, '{not json') is None
    assert request_json_object(json_headers, '{"page": 1, "nested": {"token": "x"}}') is None
    assert request_json_object(json_headers, '{"blob": "' + 'a' * MAX_BODY_BYTES + '"}') is None


def test_browser_observation_keeps_get_evidence_unchanged(fixture_server):
    records = asyncio.run(observe(fixture_server + '/pages/s1-dev-1', None))
    assert [(record.method, record.url, record.query) for record in records] == [
        ('GET', fixture_server + '/catalog/v1/items', {'page': '1', 'per_page': '10'})]
    assert records[0].request_body is None
    assert pointer(records[0].body, '/total') == 25


def test_browser_observation_captures_post_json_evidence(fixture_server):
    records = asyncio.run(observe(fixture_server + '/pages/s2-dev-1'))
    assert [(record.method, record.request_body) for record in records] == [
        ('POST', {'page': 1, 'page_size': 10}),
        ('POST', {'page': 2, 'page_size': 10}),
    ]
    assert {record.url for record in records} == {fixture_server + '/catalog/v1/query'}
    assert [pointer(record.body, '/total') for record in records] == [20, 20]
    assert [cloud_summary(record)['request_body_shape']['page'] for record in records] == [1, 2]


_METHODS_PAGE = '''<!doctype html>
<html><body><script>
const calls = [
  ['GET', '/api/items?page=1&per_page=5', null],
  ['POST_JSON', '/api/query', {page: 1, page_size: 5}],
  ['POST_SECRET', '/api/secure', {page: 1, token: 'secret-value'}],
  ['POST_FORM', '/api/form', 'page=1'],
  ['PUT', '/api/mutate', {page: 1}],
  ['DELETE', '/api/mutate', null],
];
Promise.allSettled(calls.map(([kind, path, payload]) => {
  if (kind === 'GET') return fetch(path);
  if (kind === 'DELETE') return fetch(path, {method: 'DELETE'});
  if (kind === 'POST_FORM') return fetch(path, {method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: payload});
  return fetch(path, {method: kind === 'PUT' ? 'PUT' : 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
}));
</script></body></html>
'''


def method_boundary_app(received):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    boundary = FastAPI()

    @boundary.get('/page', response_class=HTMLResponse)
    def page():
        return _METHODS_PAGE

    @boundary.get('/api/items')
    def items():
        received.append('GET /api/items')
        return {'items': [{'id': 1}], 'total': 1, 'has_next': False}

    @boundary.post('/api/query')
    def query():
        received.append('POST /api/query')
        return {'items': [{'id': 1}], 'total': 1, 'has_next': False}

    @boundary.post('/api/secure')
    def secure():
        received.append('POST /api/secure')
        return {'items': [{'id': 1}], 'total': 1, 'has_next': False}

    @boundary.post('/api/form')
    def form():
        received.append('POST /api/form')
        return {'items': [], 'total': 0, 'has_next': False}

    @boundary.put('/api/mutate')
    def mutate_put():
        received.append('PUT /api/mutate')
        return {'ok': True}

    @boundary.delete('/api/mutate')
    def mutate_delete():
        received.append('DELETE /api/mutate')
        return {'ok': True}

    return boundary


def test_observation_boundary_blocks_other_methods_and_keeps_evidence_clean():
    received = []
    base_url, stop = serve_app(method_boundary_app(received))
    try:
        records = asyncio.run(observe(base_url + '/page', None))
    finally:
        stop()
    assert sorted((record.method, record.url) for record in records) == [
        ('GET', base_url + '/api/items'), ('POST', base_url + '/api/query')]
    assert next(record for record in records if record.method == 'GET').request_body is None
    assert next(record for record in records if record.method == 'POST').request_body == {
        'page': 1, 'page_size': 5}
    # 断在浏览器路由层：表单 POST、PUT、DELETE 根本没有到达服务端。
    assert sorted(received) == ['GET /api/items', 'POST /api/query', 'POST /api/secure']
    # 敏感请求体可以发出（同源 JSON POST），但绝不进入证据或摘要。
    assert 'POST /api/secure' in received
    summaries = json.dumps([cloud_summary(record) for record in records], ensure_ascii=False)
    assert 'secret-value' not in summaries and 'token' not in summaries
