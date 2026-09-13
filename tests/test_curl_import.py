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


# --- M4.3.3 POST JSON import --------------------------------------------
#
# 导入边界从「GET」放宽到「GET + POST JSON body」。放宽不能溢出：表单、
# multipart、非 JSON 请求体、敏感键名与不支持的方法都必须被拒绝。

def test_post_json_curl_import_parses_method_and_body():
    text = ("curl 'http://127.0.0.1:8100/catalog/v1/query' -X POST "
            "-H 'Content-Type: application/json' -H 'Accept: application/json' "
            "--data-raw '{\"page\": 1, \"page_size\": 10}'")
    value = parse_curl(text)
    assert value['executable'] is True
    assert value['method'] == 'POST'
    assert value['url'] == 'http://127.0.0.1:8100/catalog/v1/query'
    assert value['query'] == {}
    assert value['request_body'] == {'page': 1, 'page_size': 10}


def test_data_flag_defaults_to_post_like_curl():
    value = parse_curl("curl http://127.0.0.1:8100/catalog/v1/query "
                       "-H 'Content-Type: application/json' "
                       "-d '{\"page\": 1, \"page_size\": 5}'")
    assert value['executable'] is True
    assert value['method'] == 'POST'
    assert value['request_body'] == {'page': 1, 'page_size': 5}


def test_get_curl_import_is_unchanged():
    value = parse_curl("curl 'http://127.0.0.1:8000/api/products?page=1&page_size=10' "
                       "-H 'Accept: application/json' --compressed")
    assert value['executable'] is True
    assert value['method'] == 'GET'
    assert value['query'] == {'page': '1', 'page_size': '10'}
    assert 'request_body' not in value


def test_post_without_json_body_is_not_executable():
    value = parse_curl("curl http://127.0.0.1:8100/catalog/v1/query -X POST "
                       "-H 'Content-Type: application/json'")
    assert value['executable'] is False


@pytest.mark.parametrize('text', [
    # 表单内容类型 / 非 JSON 请求体 / 空对象
    "curl http://127.0.0.1:8100/catalog/v1/query -H 'Content-Type: application/x-www-form-urlencoded' --data 'page=1'",
    "curl http://127.0.0.1:8100/catalog/v1/query -H 'Content-Type: application/json' --data 'not-json'",
    "curl http://127.0.0.1:8100/catalog/v1/query -H 'Content-Type: application/json' --data '[1, 2]'",
    "curl http://127.0.0.1:8100/catalog/v1/query -H 'Content-Type: application/json' --data '{}'",
    # 未显式声明 JSON 内容类型
    "curl http://127.0.0.1:8100/catalog/v1/query --data '{\"page\": 1}'",
    # 不支持的方法
    "curl http://127.0.0.1:8100/catalog/v1/query -X PUT -H 'Content-Type: application/json' --data '{\"page\": 1}'",
    # GET 携带请求体
    "curl http://127.0.0.1:8000/api/products -X GET -H 'Content-Type: application/json' --data '{\"page\": 1}'",
])
def test_unsupported_post_curls_are_rejected(text):
    assert parse_curl(text)['executable'] is False


@pytest.mark.parametrize('text', [
    "curl http://127.0.0.1:8100/catalog/v1/query -F 'page=1'",
    "curl http://127.0.0.1:8100/catalog/v1/query -H 'Content-Type: application/json' --data @payload.json",
    "curl http://127.0.0.1:8100/catalog/v1/query -H 'Content-Type: application/json' --data-binary @payload.json",
])
def test_multipart_and_file_bodies_are_rejected_at_parse_time(text):
    with pytest.raises(ValueError):
        parse_curl(text)


def test_post_sensitive_body_keys_are_rejected_and_not_echoed():
    value = parse_curl("curl http://127.0.0.1:8100/catalog/v1/query -X POST "
                       "-H 'Content-Type: application/json' "
                       "--data '{\"page\": 1, \"api_key\": \"secret-value\"}'")
    assert value['executable'] is False
    assert 'secret-value' not in str(value)


def test_imported_post_observation_replays_json_body(monkeypatch):
    import asyncio
    import json as jsonlib
    import httpx
    from backend.app.pipeline import curl_import
    original = httpx.AsyncClient

    def handler(request):
        assert request.method == 'POST'
        assert request.headers['content-type'] == 'application/json'
        assert jsonlib.loads(request.content) == {'page': 1, 'page_size': 10}
        return httpx.Response(200, json={'items': [{'sku': 1}], 'total': 1, 'has_next': False},
                              headers={'content-type': 'application/json'})

    monkeypatch.setattr(curl_import.httpx, 'AsyncClient',
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    records = asyncio.run(curl_import.observe_imported(
        'http://127.0.0.1:8100/catalog/v1/query', method='POST',
        request_body={'page': 1, 'page_size': 10}))
    assert records[0].method == 'POST'
    assert records[0].request_body == {'page': 1, 'page_size': 10}
    assert records[0].url == 'http://127.0.0.1:8100/catalog/v1/query'
    assert records[0].body['total'] == 1
