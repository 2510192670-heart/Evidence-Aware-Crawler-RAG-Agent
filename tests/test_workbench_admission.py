"""Workbench admission uses real validation/storage; worker avoids network IO."""
import json

from fastapi.testclient import TestClient

from backend.app.llm.config import CloudConfig
from backend.app.main import create_app
from backend.app.policy import AllowRule, TargetPolicy, policy_sha256


POLICY = TargetPolicy(mode='public_http', allowlist=(AllowRule(domain='example.com'),))
REFERENCE = policy_sha256(POLICY)


def config():
    return CloudConfig('https://model.example', 'test-secret', 'test-model')


async def worker(spec, config, task_id, root, stage):
    return {'status': 'succeeded', 'count': 0}


def test_policy_discovery(tmp_path):
    with TestClient(create_app(tmp_path, config, worker, policies=[POLICY])) as client:
        response = client.get('/api/v1/policies')
        assert response.status_code == 200
        assert response.json()['items'] == [
            {'sha256': REFERENCE, 'mode': 'public_http', 'domains': ['example.com']}]


def test_public_curl_preview_and_admission(tmp_path):
    with TestClient(create_app(tmp_path, config, worker, policies=[POLICY])) as client:
        preview = client.post('/api/v1/import/curl', json={
            'text': "curl 'https://example.com/api?page=1'", 'policy_sha256': REFERENCE})
        assert preview.status_code == 200
        assert preview.json()['executable'] is True
        response = client.post('/api/v1/tasks', json={
            'url': 'https://example.com/', 'policy_sha256': REFERENCE,
            'imported_url': preview.json()['url'], 'fields': ['id']})
        assert response.status_code == 202, response.json()
        assert response.json()['spec']['imported_url'] == 'https://example.com/api?page=1'


def test_public_import_rejected_before_persistence(tmp_path):
    with TestClient(create_app(tmp_path, config, worker, policies=[POLICY])) as client:
        for imported in ['https://evil.example/api?page=1',
                         'https://example.com/api?access_token=secret',
                         'https://example.com/api?page=1&page=2']:
            response = client.post('/api/v1/tasks', json={
                'url': 'https://example.com/', 'policy_sha256': REFERENCE,
                'imported_url': imported})
            assert response.status_code == 422
        assert client.get('/api/v1/tasks').json()['total'] == 0


def test_unknown_policy_import_rejected(tmp_path):
    with TestClient(create_app(tmp_path, config, worker)) as client:
        response = client.post('/api/v1/import/curl', json={
            'text': "curl 'https://example.com/api?page=1'", 'policy_sha256': 'f' * 64})
        assert response.status_code == 422
        assert response.json()['code'] == 'policy_not_found'


def test_deployment_policy_file_loaded(tmp_path, monkeypatch):
    policy_file = tmp_path / 'policy.json'
    policy_file.write_text(json.dumps(POLICY.model_dump(mode='json')), encoding='utf-8')
    monkeypatch.setenv('WDA_TARGET_POLICY_FILE', str(policy_file))
    with TestClient(create_app(tmp_path / 'data', config, worker)) as client:
        response = client.get('/api/v1/policies')
        assert response.status_code == 200
        assert response.json()['items'][0]['sha256'] == REFERENCE
