import json

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.llm.config import CloudConfig


def default_config():
    return CloudConfig('https://example.test', 'default-test-key', 'default-model')


async def worker(spec, config, task_id, root, stage):
    return {'status': 'succeeded', 'selected_model': config.model}


def test_profile_selection_and_secret_is_not_returned_or_persisted(tmp_path):
    with TestClient(create_app(tmp_path, default_config, worker)) as client:
        created = client.post('/api/v1/models', json={'name': 'My model',
                              'base_url': 'https://provider.example/v1', 'model': 'my-model',
                              'api_key': 'test-private-credential'})
        assert created.status_code == 201
        profile = created.json()
        listing = client.get('/api/v1/models').json()
        assert 'test-private-credential' not in json.dumps([profile, listing])
        assert listing['items'][0]['id'] == profile['id']
        response = client.post('/api/v1/tasks', json={'model_profile': profile['id']})
        assert response.status_code == 202, response.text
        assert response.json()['model'] == 'my-model'
        assert 'test-private-credential' not in response.text
    assert b'test-private-credential' not in (tmp_path / 'agent.sqlite3').read_bytes()


def test_unknown_profile_rejected_before_creation(tmp_path):
    with TestClient(create_app(tmp_path, default_config, worker)) as client:
        response = client.post('/api/v1/tasks', json={'model_profile': 'unknown'})
        assert response.status_code == 422
        assert response.json()['code'] == 'model_profile_not_found'
        assert client.get('/api/v1/tasks').json()['total'] == 0


def test_model_profile_invalid_config_does_not_echo_secret(tmp_path):
    with TestClient(create_app(tmp_path, default_config, worker)) as client:
        response = client.post('/api/v1/models', json={'name': 'Invalid',
                               'base_url': 'http://provider.example', 'model': 'm', 'api_key': 'secret-value'})
        assert response.status_code == 422
        assert 'secret-value' not in response.text


def test_explicit_profile_probe_and_removal(tmp_path, monkeypatch):
    from backend.app.llm import profiles
    from types import SimpleNamespace
    calls = []
    class Gateway:
        def __init__(self, config):
            self.config, self.calls = config, 0
        async def generate(self, payload, schema):
            calls.append(self.config.model)
            self.calls += 1
            return SimpleNamespace(data=schema(status='ok'))
    monkeypatch.setattr(profiles, 'CloudGateway', Gateway)
    with TestClient(create_app(tmp_path, default_config, worker)) as client:
        profile = client.post('/api/v1/models', json={'name': 'Check', 'base_url': 'https://example.test',
                              'model': 'check-model', 'api_key': 'private-check-key'}).json()
        assert calls == []
        response = client.post(f"/api/v1/models/{profile['id']}/check")
        assert response.status_code == 200
        assert response.json()['model_calls'] == 1
        assert calls == ['check-model']
        assert client.delete(f"/api/v1/models/{profile['id']}").status_code == 200
        assert client.get('/api/v1/models').json()['items'] == []
