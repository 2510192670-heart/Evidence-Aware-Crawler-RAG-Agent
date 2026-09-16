import time

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.llm.config import CloudConfig


def config():
    return CloudConfig('https://model.example', 'fixture-key', 'fixture-model')


async def worker(spec, config, task_id, root, stage):
    await stage('analyzing')
    await stage.preview({'plan_hash': 'a' * 64, 'items': [{'id': 1}], 'count': 1})
    await stage('executing')
    return {'status': 'succeeded', 'count': 1, 'model_calls': 1}


def wait_for(client, task_id, predicate):
    for _ in range(200):
        row = client.get('/api/v1/tasks/' + task_id).json()
        if predicate(row):
            return row
        time.sleep(.01)
    raise AssertionError(row)


def test_preview_requires_matching_confirmation_and_releases_worker(tmp_path):
    with TestClient(create_app(tmp_path, config, worker)) as client:
        result = client.post('/api/v1/tasks', json={'preview': True})
        assert result.status_code == 202
        task_id = result.json()['id']
        row = wait_for(client, task_id, lambda r: bool(r['summary'].get('preview')))
        assert row['status'] == 'analyzing'
        assert row['summary']['preview']['items'] == [{'id': 1}]
        assert client.post('/api/v1/tasks', json={}).status_code == 409
        assert client.post(f'/api/v1/tasks/{task_id}/confirm', json={'plan_hash': 'b'*64}).status_code == 409
        assert client.post(f'/api/v1/tasks/{task_id}/confirm', json={'plan_hash': 'a'*64}).status_code == 200
        final = wait_for(client, task_id, lambda r: r['status'] == 'succeeded')
        assert final['summary']['model_calls'] == 1
        assert client.post(f'/api/v1/tasks/{task_id}/confirm', json={'plan_hash': 'a'*64}).status_code == 409


def test_preview_can_be_cancelled(tmp_path):
    with TestClient(create_app(tmp_path, config, worker)) as client:
        response = client.post('/api/v1/tasks', json={'preview': True})
        assert response.status_code == 202
        task_id = response.json()['id']
        wait_for(client, task_id, lambda r: bool(r['summary'].get('preview')))
        assert client.post(f'/api/v1/tasks/{task_id}/cancel').json()['status'] == 'cancelled'
        assert client.post(f'/api/v1/tasks/{task_id}/confirm', json={'plan_hash': 'a'*64}).status_code == 409


def test_preview_is_interrupted_on_restart_instead_of_replayed(tmp_path):
    from backend.app.storage.repository import Repository
    repo = Repository(tmp_path)
    repo.initialize()
    task = repo.create({'preview': True}, 'fixture')
    repo.transition(task['id'], 'analyzing')
    repo.update_preview(task['id'], {'plan_hash': 'a'*64, 'items': [{'id': 1}]})
    repo.close()
    restarted = Repository(tmp_path)
    try:
        restarted.initialize()
        restarted.recover()
        row = restarted.get(task['id'])
        assert row['status'] == 'interrupted'
        assert row['summary'] == {'status': 'interrupted', 'error': 'process_interrupted'}
    finally:
        restarted.close()
