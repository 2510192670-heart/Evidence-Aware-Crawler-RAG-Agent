import asyncio
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.llm.config import CloudConfig
from backend.app.storage.repository import Repository, BusyError


SPEC = {'url': 'http://127.0.0.1:8000/', 'fields': ['id', 'name', 'price_fen'], 'max_pages': 3, 'click_text': '下一页'}


def config():
    return CloudConfig('https://example.test', 'not-a-real-secret', 'fake-model')


async def fast_worker(spec, cfg, task_id, root, stage):
    for name in ['observing', 'analyzing', 'executing', 'verifying']:
        await stage(name)
    folder = root / 'tasks' / task_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'result.json').write_text('[{"id":1}]', encoding='utf-8')
    (folder / 'evidence.json').write_text('[]', encoding='utf-8')
    report = {'status': 'succeeded', 'count': 1, 'model_calls': 0, 'completeness': 'complete'}
    (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    return report


def wait_terminal(client, task_id):
    for _ in range(100):
        task = client.get(f'/api/v1/tasks/{task_id}').json()
        if task['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
            return task
        time.sleep(0.01)
    pytest.fail('Task did not finish')


def test_repository_persistence_slot_events_and_recovery(tmp_path):
    repo = Repository(tmp_path)
    repo.initialize()
    first = repo.create(SPEC, 'fake-model')
    with pytest.raises(BusyError):
        repo.create(SPEC, 'fake-model')
    repo.transition(first['id'], 'observing')
    repo.close()
    reopened = Repository(tmp_path)
    reopened.initialize()
    reopened.recover()
    assert reopened.get(first['id'])['status'] == 'interrupted'
    reopened.transition(first['id'], 'succeeded')
    assert reopened.get(first['id'])['status'] == 'interrupted'
    assert [e['seq'] for e in reopened.events(first['id'])] == [1, 2, 3]
    second = reopened.create(SPEC, 'fake-model')
    assert second['id'] != first['id']
    reopened.close()


def test_create_poll_download_and_reload(tmp_path):
    with TestClient(create_app(tmp_path, config, fast_worker)) as client:
        response = client.post('/api/v1/tasks', json=SPEC)
        assert response.status_code == 202
        task_id = response.json()['id']
        task = wait_terminal(client, task_id)
        assert task['status'] == 'succeeded'
        assert task['summary']['count'] == 1
        assert 'not-a-real-secret' not in json.dumps(task)
        events = client.get(f'/api/v1/tasks/{task_id}/events').json()['items']
        assert [e['status'] for e in events] == ['created', 'observing', 'analyzing', 'executing', 'verifying', 'succeeded']
        assert client.get(f'/api/v1/tasks/{task_id}/events?after_seq=6').json()['items'] == []
        artifacts = client.get(f'/api/v1/tasks/{task_id}/artifacts').json()['items']
        result = next(a for a in artifacts if a['filename'] == 'result.json')
        download = client.get(f'/api/v1/artifacts/{result["id"]}/download')
        assert download.status_code == 200
        assert download.json() == [{'id': 1}]
        assert hashlib.sha256(download.content).hexdigest() == result['sha256']
        assert client.get(f'/api/v1/tasks/{task_id}/evidence').json() == []
        assert client.post(f'/api/v1/tasks/{task_id}/cancel').json()['status'] == 'succeeded'
    with TestClient(create_app(tmp_path, config, fast_worker)) as client:
        assert client.get('/api/v1/tasks').json()['total'] == 1
        assert client.get(f'/api/v1/tasks/{task_id}').json()['status'] == 'succeeded'


def test_busy_cancel_waits_for_cleanup_and_is_idempotent(tmp_path):
    cleaned = []
    async def worker(spec, cfg, task_id, root, stage):
        try:
            await stage('observing')
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep(0.02)
            cleaned.append(task_id)
    with TestClient(create_app(tmp_path, config, worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        for _ in range(100):
            if client.get(f'/api/v1/tasks/{task_id}').json()['status'] == 'observing':
                break
            time.sleep(0.01)
        assert client.post('/api/v1/tasks', json=SPEC).status_code == 409
        assert client.get(f'/api/v1/tasks/{task_id}/artifacts').json()['items'] == []
        cancelled = client.post(f'/api/v1/tasks/{task_id}/cancel')
        assert cancelled.json()['status'] == 'cancelled'
        assert task_id in cleaned
        assert client.post(f'/api/v1/tasks/{task_id}/cancel').json()['status'] == 'cancelled'
        assert client.post('/api/v1/tasks', json=SPEC).status_code == 202


def test_worker_error_and_missing_configuration(tmp_path):
    async def failing(*args):
        raise RuntimeError('not-a-real-secret')
    with TestClient(create_app(tmp_path, config, failing)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        task = wait_terminal(client, task_id)
        assert task['status'] == 'failed'
        assert 'not-a-real-secret' not in json.dumps(task)
    def missing():
        raise ValueError('missing')
    with TestClient(create_app(tmp_path, missing, failing)) as client:
        assert client.post('/api/v1/tasks', json=SPEC).status_code == 503
        assert client.get('/api/v1/health').json()['model']['configured'] is False


@pytest.mark.parametrize('patch', [{'url': 'https://example.com'}, {'url': 'http://127.0.0.1:8000/?token=secret'}, {'max_pages': 11}, {'fields': []}, {'fields': ['id', 'id']}])
def test_request_validation(tmp_path, patch):
    with TestClient(create_app(tmp_path, config, fast_worker)) as client:
        assert client.post('/api/v1/tasks', json={**SPEC, **patch}).status_code == 422


def test_cross_site_and_missing_artifact(tmp_path):
    with TestClient(create_app(tmp_path, config, fast_worker)) as client:
        assert client.post('/api/v1/tasks', json=SPEC, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/api/v1/tasks/not-found').status_code == 404
        assert client.get('/api/v1/artifacts/not-found/download').status_code == 404


def test_tampered_artifact_rejected(tmp_path):
    with TestClient(create_app(tmp_path, config, fast_worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        wait_terminal(client, task_id)
        artifacts = client.get(f'/api/v1/tasks/{task_id}/artifacts').json()['items']
        artifact = next(a for a in artifacts if a['filename'] == 'result.json')
        (tmp_path / 'tasks' / task_id / 'result.json').write_text('tampered', encoding='utf-8')
        assert client.get(f'/api/v1/artifacts/{artifact["id"]}/download').status_code == 409


def test_same_data_directory_cannot_start_second_api(tmp_path):
    with TestClient(create_app(tmp_path, config, fast_worker)):
        with pytest.raises(RuntimeError):
            with TestClient(create_app(tmp_path, config, fast_worker)):
                pytest.fail('Second API should not acquire instance lock')


def test_shutdown_cancels_active_task_and_persists(tmp_path):
    async def worker(spec, cfg, task_id, root, stage):
        await stage('observing')
        await asyncio.sleep(30)
    with TestClient(create_app(tmp_path, config, worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
    with TestClient(create_app(tmp_path, config, fast_worker)) as client:
        assert client.get(f'/api/v1/tasks/{task_id}').json()['status'] == 'cancelled'


def test_cancel_before_worker_runs_releases_slot(tmp_path):
    from backend.app.tasks.service import TaskService
    repo = Repository(tmp_path)
    repo.initialize()
    async def worker(*args):
        await asyncio.sleep(30)
    async def scenario():
        service = TaskService(repo, config, worker)
        row = repo.create(SPEC, 'fake-model')
        handle = asyncio.create_task(service._drive(row, config()))
        service.handles[row['id']] = handle
        # 确定在协程初次执行前取消，不依赖调度时间。
        handle.cancel()
        assert (await service.cancel(row['id']))['status'] == 'cancelled'
        assert repo.create(SPEC, 'fake-model')['status'] == 'created'
    asyncio.run(scenario())
    repo.close()
