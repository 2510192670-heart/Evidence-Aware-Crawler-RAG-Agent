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
