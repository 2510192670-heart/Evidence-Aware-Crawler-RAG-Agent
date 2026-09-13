import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.llm.config import CloudConfig
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.pipeline.execution import execute_plan
from backend.app.pipeline.export import build_collector_source, compare_exported_result, export_collector
from backend.app.storage.repository import Repository
from benchmarks import load as load_manifest
from fixtures.app import app as fixture_app


def record(url='http://127.0.0.1:8000/api/products', query=None, request_id='req_001'):
    return Observation(request_id=request_id, url=url, query=query or {'page': '1', 'page_size': '10'},
                       body={'total': 30, 'has_next': True, 'items': [{'id': 1, 'name': 'x', 'price_fen': 100}]})


def plan(**overrides):
    values = dict(request_id='req_001', items_pointer='/items',
                  fields={'id': '/id', 'name': '/name', 'price_fen': '/price_fen'},
                  unique_key='id', page_parameter='page', has_next_pointer='/has_next', total_pointer='/total')
    values.update(overrides)
    return ExtractionPlan(**values)


# --- Unit ---------------------------------------------------------------

def test_build_collector_source_is_deterministic_and_embeds_plan():
    observation = record()
    first = build_collector_source(plan(), observation, 3)
    assert first == build_collector_source(plan(), observation, 3)
    compile(first, 'collector.py', 'exec')
    assert 'urllib.request' in first
    assert '127.0.0.1:8000/api/products' in first
    assert '"page"' in first and '"/items"' in first and '"/price_fen"' in first
    assert 'Authorization' not in first and 'Cookie' not in first
    assert 'import httpx' not in first


@pytest.mark.parametrize('overrides,observation', [
    ({'request_id': 'invented'}, record()),
    ({'page_parameter': 'invented'}, record()),
    ({'fields': {'id': '/id', 'api_key': '/secret'}, 'unique_key': 'id'}, record()),
    ({}, record(query={'page': '1', 'token': 'secret'})),
    ({}, record(url='http://localhost:8000/api/products')),
])
def test_build_collector_rejects_invalid_or_unvalidated_plan(overrides, observation):
    with pytest.raises(ValueError):
        build_collector_source(plan(**overrides), observation, 3)


def test_build_collector_rejects_bad_page_limit():
    with pytest.raises(ValueError):
        build_collector_source(plan(), record(), 0)


def test_compare_exported_result_rules():
    internal = {'items': [{'id': 2, 'name': 'b'}, {'id': 1, 'name': 'a'}],
                'pages': 2, 'expected_total': 2, 'completeness': 'complete'}
    same_order_free = {'items': [{'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}],
                       'pages': 2, 'expected_total': 2, 'completeness': 'complete'}
    assert compare_exported_result(internal, same_order_free, 'id')['matches'] is True
    assert compare_exported_result(internal, {'items': [{'id': 1, 'name': 'a'}], 'pages': 1,
                                              'expected_total': 2, 'completeness': 'partial'}, 'id')['matches'] is False
    assert compare_exported_result(internal, {'items': [{'id': 1, 'name': 1}, {'id': 2, 'name': 'b'}],
                                              'pages': 2, 'expected_total': 2, 'completeness': 'complete'}, 'id')['matches'] is False
    assert compare_exported_result(internal, None, 'id')['matches'] is False


def test_validation_rules_are_shared_with_execution():
    # 导出校验与执行前校验使用同一套 loopback / 敏感字段规则。
    from backend.app.pipeline.contracts import local_origin, SENSITIVE
    assert local_origin('http://127.0.0.1:8000/x') == 'http://127.0.0.1:8000'
    assert SENSITIVE.search('api_key')


# --- Real loopback 靶场 --------------------------------------------------

@pytest.fixture(scope='module')
def shop_server():
    import uvicorn
    from main import app
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
        pytest.fail('loopback test server did not start')
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True
    thread.join(timeout=10)


def test_exported_collector_matches_internal_execution(shop_server, tmp_path):
    url = shop_server + '/api/products'
    with httpx.Client(trust_env=False) as client:
        body = client.get(url, params={'page': 1, 'page_size': 10}).json()
    observation = Observation(request_id='req_001', url=url, query={'page': '1', 'page_size': '10'}, body=body)

    async def scenario():
        async with httpx.AsyncClient(trust_env=False) as client:
            internal = await execute_plan(client, plan(), [observation], max_pages=3)
            fragment = await export_collector(tmp_path, plan(), observation, internal, max_pages=3)
            return internal, fragment

    internal, fragment = asyncio.run(scenario())
    assert internal['completeness'] == 'complete'
    assert len(internal['items']) == 30 and internal['pages'] == 3
    assert fragment['collector_exported'] is True
    assert fragment['collector_execution_success'] is True
    assert fragment['collector_matches_internal_result'] is True
    exported = json.loads((tmp_path / 'collector_result.json').read_text(encoding='utf-8'))
    assert len(exported['items']) == 30 and exported['pages'] == 3 and exported['completeness'] == 'complete'
    assert exported['items'] == internal['items']
    assert (tmp_path / 'collector.py').is_file()


def test_run_task_writes_and_registers_collector(shop_server, tmp_path, monkeypatch):
    observation = record(url=shop_server + '/api/products')

    class Gateway:
        calls = 0
        usage_history = []
        def __init__(self, config):
            pass
        async def generate(self, payload, schema):
            self.calls += 1
            return SimpleNamespace(data=plan())

    async def observe(*args):
        return [observation]

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    args = SimpleNamespace(url=shop_server + '/', click_text='', fields='id,name,price_fen',
                           max_pages=3, rag_enabled=False)
    assert asyncio.run(run.run_task(args, SimpleNamespace(model='fake'), output_root=tmp_path, quiet=True)) == 0
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['collector_exported'] is True
    assert report['collector_execution_success'] is True
    assert report['collector_matches_internal_result'] is True
    assert (folder / 'collector.py').is_file()
    assert (folder / 'collector_result.json').is_file()
    assert (folder / 'collector_verification.json').is_file()


def test_collector_artifacts_are_registered(tmp_path):
    repo = Repository(tmp_path)
    repo.initialize()
    row = repo.create(SPEC, 'fake-model')
    folder = tmp_path / 'tasks' / row['id']
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'collector.py').write_text('print(1)\n', encoding='utf-8')
    (folder / 'collector_result.json').write_text('{"items": []}\n', encoding='utf-8')
    (folder / 'collector_verification.json').write_text('{"matches": true}\n', encoding='utf-8')
    repo.register_artifacts(row['id'])
    names = {entry['filename'] for entry in repo.artifacts(row['id'])}
    assert {'collector.py', 'collector_result.json', 'collector_verification.json'} <= names
    repo.close()


def test_export_failure_does_not_fail_task(shop_server, tmp_path, monkeypatch):
    observation = record(url=shop_server + '/api/products')

    class Gateway:
        calls = 0
        usage_history = []
        def __init__(self, config):
            pass
        async def generate(self, payload, schema):
            self.calls += 1
            return SimpleNamespace(data=plan())

    async def observe(*args):
        return [observation]

    async def broken_export(*args, **kwargs):
        raise RuntimeError('collector export boom')

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(run, 'export_collector', broken_export)
    args = SimpleNamespace(url=shop_server + '/', click_text='', fields='id,name,price_fen',
                           max_pages=3, rag_enabled=False)
    assert asyncio.run(run.run_task(args, SimpleNamespace(model='fake'), output_root=tmp_path, quiet=True)) == 0
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'succeeded'
    assert report['collector_exported'] is False
    assert report['collector_matches_internal_result'] is False


# --- API / artifact ------------------------------------------------------

SPEC = {'url': 'http://127.0.0.1:8000/', 'fields': ['id', 'name', 'price_fen'], 'max_pages': 3, 'click_text': '下一页'}


def config():
    return CloudConfig('https://example.test', 'not-a-real-secret', 'fake-model')


def test_collector_artifact_downloads_as_python(tmp_path):
    async def worker(spec, cfg, task_id, root, stage):
        folder = root / 'tasks' / task_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'collector.py').write_text('print(1)\n', encoding='utf-8')
        (folder / 'result.json').write_text('[]', encoding='utf-8')
        report = {'status': 'succeeded', 'count': 0, 'model_calls': 0, 'completeness': 'complete'}
        (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        return report

    with TestClient(create_app(tmp_path, config, worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        for _ in range(100):
            if client.get(f'/api/v1/tasks/{task_id}').json()['status'] == 'succeeded':
                break
            time.sleep(0.01)
        artifacts = client.get(f'/api/v1/tasks/{task_id}/artifacts').json()['items']
        collector = next(a for a in artifacts if a['filename'] == 'collector.py')
        download = client.get(f'/api/v1/artifacts/{collector["id"]}/download')
        assert download.status_code == 200
        assert download.headers['content-type'].startswith('text/x-python')
        assert download.text.replace('\r\n', '\n') == 'print(1)\n'


# --- M4.3.3 POST JSON collector -----------------------------------------
#
# POST 走独立模板：standalone、仅标准库、回环、application/json、只修改页码成员，
# 且输出 schema 与 GET 采集器逐字段一致。GET 模板与输出保持不变。

def post_record(url='http://127.0.0.1:8100/catalog/v1/query', request_body=None, request_id='req_001'):
    return Observation(request_id=request_id, url=url, method='POST', query={},
                       request_body=request_body or {'page': 1, 'page_size': 10},
                       body={'total': 20, 'has_next': True,
                             'items': [{'sku': 1, 'title': 'x', 'amount_fen': 100}]})


def post_plan(**overrides):
    values = dict(request_id='req_001', items_pointer='/items',
                  fields={'sku': '/sku', 'title': '/title', 'amount_fen': '/amount_fen'},
                  unique_key='sku', pagination_location='json_body', page_parameter='page',
                  has_next_pointer='/has_next', total_pointer='/total')
    values.update(overrides)
    return ExtractionPlan(**values)


def test_build_post_collector_source_is_deterministic_and_standalone():
    observation = post_record()
    first = build_collector_source(post_plan(), observation, 2)
    assert first == build_collector_source(post_plan(), observation, 2)
    compile(first, 'collector.py', 'exec')
    assert 'urllib.request' in first and 'import httpx' not in first
    assert "method='POST'" in first
    assert "'Content-Type': 'application/json'" in first
    assert '"request_body"' in first and '"page"' in first and '"/items"' in first
    assert 'Authorization' not in first and 'Cookie' not in first


def test_post_collector_uses_its_own_template_and_keeps_get_output_stable():
    get_source = build_collector_source(plan(), record(), 3)
    assert '"query"' in get_source and 'request_body' not in get_source
    post_source = build_collector_source(post_plan(), post_record(), 3)
    assert '"request_body"' in post_source and '"query"' not in post_source
    assert get_source != post_source


@pytest.mark.parametrize('overrides,observation', [
    ({'page_parameter': 'invented'}, post_record()),
    ({}, post_record(request_body={'page': '1', 'page_size': 10})),
    ({'pagination_location': 'query'}, post_record()),
    ({'pagination_location': 'json_body'}, record()),
    ({}, post_record(url='http://localhost:8100/catalog/v1/query')),
    ({}, post_record(request_body={'page': 1, 'api_key': 'secret'})),
])
def test_build_post_collector_rejects_invalid_or_unvalidated_plan(overrides, observation):
    with pytest.raises(ValueError):
        build_collector_source(post_plan(**overrides), observation, 3)


@pytest.fixture(scope='module')
def fixtures_server():
    """在回环端口运行 M4.1 靶场，供 S2 POST 真实执行。"""
    import uvicorn
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fixture_app, host='127.0.0.1', port=port, log_level='warning'))
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


POST_TASKS = [task for task in load_manifest()['tasks'] if task['solution']['method'] == 'POST']


def task_observation_and_plan(base_url, task):
    solution = task['solution']
    request_body = {solution['pagination_parameter']: 1, solution['page_size_parameter']: task['page_size']}
    with httpx.Client(trust_env=False) as client:
        body = client.post(base_url + solution['request_path'], json=request_body).json()
    observation = Observation(request_id='req_001', url=base_url + solution['request_path'],
                              method='POST', query={}, request_body=request_body, body=body)
    extraction = ExtractionPlan(request_id='req_001', items_pointer=solution['items_pointer'],
                                fields={name: '/' + name for name in task['fields']},
                                unique_key=solution['unique_key'], pagination_location='json_body',
                                page_parameter=solution['pagination_parameter'],
                                has_next_pointer=solution['has_next_pointer'],
                                total_pointer=solution['total_pointer'])
    return observation, extraction


@pytest.mark.parametrize('task', POST_TASKS, ids=[t['id'] for t in POST_TASKS])
def test_exported_post_collector_matches_internal_execution(fixtures_server, task, tmp_path):
    observation, extraction = task_observation_and_plan(fixtures_server, task)

    async def scenario():
        async with httpx.AsyncClient(trust_env=False) as client:
            internal = await execute_plan(client, extraction, [observation], task['max_pages'])
            fragment = await export_collector(tmp_path, extraction, observation, internal, task['max_pages'])
            return internal, fragment

    internal, fragment = asyncio.run(scenario())
    expected = task['expected']
    assert internal['completeness'] == 'complete', task['id']
    assert len(internal['items']) == expected['record_count'], task['id']
    assert internal['pages'] == expected['pages'], task['id']
    assert fragment['collector_exported'] is True
    assert fragment['collector_execution_success'] is True
    assert fragment['collector_matches_internal_result'] is True

    # 逐项核对：count / pages / completeness / records / 字段名 / 字段类型 / 唯一键。
    verification = json.loads((tmp_path / 'collector_verification.json').read_text(encoding='utf-8'))
    checks = verification['checks']
    assert checks['record_count'] and checks['completeness'] and checks['expected_total'], task['id']
    assert checks['field_names'] and checks['field_types'] and checks['unique_keys'], task['id']
    assert checks['records'] is True, task['id']

    exported = json.loads((tmp_path / 'collector_result.json').read_text(encoding='utf-8'))
    assert exported['items'] == internal['items'], task['id']
    assert set(exported['items'][0]) == set(task['fields']), task['id']

    collector = (tmp_path / 'collector.py').read_text(encoding='utf-8')
    assert 'import httpx' not in collector
    assert '"request_body"' in collector
