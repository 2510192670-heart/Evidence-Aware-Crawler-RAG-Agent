import pytest
from backend.app.pipeline.curl_import import parse_curl


def test_basic_bash_curl():
    value = parse_curl("curl 'http://127.0.0.1:8000/api/products?page=1&page_size=10' \\\n -H 'Accept: application/json' --compressed")
    assert value['executable'] is True
    assert value['query'] == {'page': '1', 'page_size': '10'}


@pytest.mark.parametrize('text', ["curl http://example.com", "curl http://127.0.0.1/ -X POST", "curl http://127.0.0.1/ -H 'Cookie: abc=secret'", "curl 'http://127.0.0.1/?token=secret'", "curl http://127.0.0.1/ -H 'X-Custom: secret'"])
def test_unsupported_not_executable_and_secret_not_echoed(text):
    result = parse_curl(text)
    assert result['executable'] is False
    assert 'secret' not in str(result)


@pytest.mark.parametrize('text', ['curl http://127.0.0.1/; whoami', 'curl http://127.0.0.1/ --output file', 'curl http://127.0.0.1/ --data @file', 'curl http://127.0.0.1/ $(whoami)', 'curl http://127.0.0.1/ http://127.0.0.1/other', 'curl "unterminated'])
def test_ambiguous_or_shell_features_rejected(text):
    with pytest.raises(ValueError):
        parse_curl(text)


def test_preview_api_needs_no_key_and_does_not_create_task(tmp_path):
    from fastapi.testclient import TestClient
    from backend.app.main import create_app, TaskInput
    with TestClient(create_app(data_dir=tmp_path)) as client:
        response = client.post('/api/v1/import/curl', json={'text': 'curl http://127.0.0.1:8000/api/products?page=1'})
        assert response.status_code == 200
        assert response.json()['executable']
        assert client.get('/api/v1/tasks').json()['total'] == 0
        assert client.post('/api/v1/import/curl', json={'text': 'curl --data @secret'}).status_code == 422
    with pytest.raises(ValueError):
        TaskInput(imported_url='http://127.0.0.1/?token=secret')


def test_imported_observation_and_pipeline_selection(monkeypatch):
    import asyncio
    import httpx
    from backend.app.pipeline import curl_import
    original = httpx.AsyncClient
    def handler(request):
        assert str(request.url) == 'http://127.0.0.1/api/data?page=1'
        return httpx.Response(200, json={'items': [{'id': 1}]})
    monkeypatch.setattr(curl_import.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    records = asyncio.run(curl_import.observe_imported('http://127.0.0.1/api/data?page=1'))
    assert records[0].query == {'page': '1'}
    assert records[0].url == 'http://127.0.0.1/api/data'
