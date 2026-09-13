"""Fixture behaviour and current-pipeline compatibility for the M4.1 targets.

These tests are the answer-key verification: they prove the deterministic
fixtures behave exactly as ``benchmarks/tasks.json`` declares, and that the
production pipeline can still only do what M3 allowed (GET page-number).
"""

import asyncio
import math
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.pipeline.contracts import ExtractionPlan, Observation, pointer
from backend.app.pipeline.curl_import import parse_curl
from backend.app.pipeline.execution import execute_plan
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
            return str(caught.value)

    assert asyncio.run(scenario()) == task['expected']['failure_kind']


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


def test_post_fixtures_are_served_but_pipeline_still_rejects_post(fixture_server):
    body = httpx.post(fixture_server + '/catalog/v1/query',
                      json={'page': 1, 'page_size': 10}, timeout=10, trust_env=False).json()
    assert body['total'] == 20 and len(body['items']) == 10

    # Observation has no method field, so a POST request cannot be expressed.
    with pytest.raises(ValidationError):
        Observation(request_id='r', url='http://127.0.0.1:8100/catalog/v1/query',
                    query={'page': '1'}, body=body, method='POST')
    # The plan has no pagination location, so a JSON-body page number cannot be expressed.
    with pytest.raises(ValidationError):
        ExtractionPlan(request_id='r', items_pointer='/items', fields={'sku': '/sku'}, unique_key='sku',
                       page_parameter='page', has_next_pointer='/has_next', total_pointer='/total',
                       location='json_body')
    # cURL import still refuses to treat POST as executable.
    assert parse_curl('curl http://127.0.0.1:8100/catalog/v1/query -X POST')['executable'] is False


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
