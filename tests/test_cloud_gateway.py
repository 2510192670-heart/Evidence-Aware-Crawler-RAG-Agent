import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from backend.app.llm.config import CloudConfig
from backend.app.llm.gateway import CloudGateway, GatewayError


class Answer(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    ok: bool


@pytest.mark.parametrize('error,code', [(httpx.ConnectTimeout, 'connect_timeout'),
    (httpx.ReadTimeout, 'read_timeout'), (httpx.WriteTimeout, 'write_timeout'),
    (httpx.PoolTimeout, 'pool_timeout')])
def test_timeout_phase_is_reported_without_exception_details(error, code):
    def handler(request):
        raise error('private-token-in-error')
    gateway = CloudGateway(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayError) as caught:
        run(gateway)
    assert caught.value.code == code
    assert 'private-token' not in str(caught.value)
    assert gateway.calls == 1


def config(**kwargs):
    return CloudConfig('https://model.example/v1', 'test-secret', 'example-model', **kwargs)


def response(content='{"ok":true}', reason='stop', usage=None):
    return httpx.Response(200, json={'choices': [{'finish_reason': reason, 'message': {'content': content}}], 'usage': usage})


def run(gateway):
    return asyncio.run(gateway.generate({'purpose': 'offline_test'}, Answer))


def test_config_missing_and_secret_hidden():
    with pytest.raises(ValueError):
        CloudConfig.from_env({})
    cfg = CloudConfig.from_env({'WDA_LLM_BASE_URL': 'https://model.example/v1/', 'WDA_LLM_API_KEY': 'test-secret', 'WDA_LLM_MODEL': 'example-model'})
    assert cfg.base_url == 'https://model.example/v1'
    assert 'test-secret' not in repr(cfg)


@pytest.mark.parametrize('url', ['http://model.example', 'https://user:secret@model.example', 'https://model.example?key=secret', 'https://model.example/#fragment', 'https://'])
def test_invalid_url(url):
    with pytest.raises(ValueError):
        CloudConfig(url, 'test-secret', 'model')


def test_request_and_usage():
    def handler(request):
        assert str(request.url) == 'https://model.example/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer test-secret'
        body = json.loads(request.content)
        assert body['model'] == 'example-model'
        assert body['response_format'] == {'type': 'json_object'}
        assert body['max_tokens'] == 1000
        assert body['stream'] is False
        assert 'JSON' in body['messages'][0]['content']
        return response(usage={'prompt_tokens': 20, 'completion_tokens': 5})
    gateway = CloudGateway(config(), transport=httpx.MockTransport(handler))
    result = run(gateway)
    assert result.data.ok is True
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 5
    assert gateway.calls == 1


def test_missing_usage_is_unknown():
    result = run(CloudGateway(config(), transport=httpx.MockTransport(lambda r: response())))
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None


@pytest.mark.parametrize('status,code', [(401, 'authentication_failed'), (403, 'access_denied'), (429, 'rate_limited'), (500, 'provider_error'), (302, 'redirect_rejected'), (400, 'request_rejected')])
def test_http_failures_do_not_retry_or_echo_body(status, code):
    gateway = CloudGateway(config(), transport=httpx.MockTransport(lambda r: httpx.Response(status, text='test-secret')))
    with pytest.raises(GatewayError) as error:
        run(gateway)
    assert error.value.code == code
    assert 'test-secret' not in str(error.value)
    assert gateway.calls == 1


@pytest.mark.parametrize('content,reason,code', [('', 'stop', 'invalid_output'), ('{', 'stop', 'invalid_output'), ('{"ok":"yes"}', 'stop', 'invalid_output'), ('{"ok":true}', 'length', 'output_incomplete'), ('{"ok":true}', 'content_filter', 'output_incomplete')])
def test_invalid_output(content, reason, code):
    gateway = CloudGateway(config(), transport=httpx.MockTransport(lambda r: response(content, reason)))
    with pytest.raises(GatewayError) as error:
        run(gateway)
    assert error.value.code == code


def test_network_timeout():
    def handler(request):
        raise httpx.ReadTimeout('test-secret')
    gateway = CloudGateway(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayError, match='timeout'):
        run(gateway)
    assert gateway.calls == 1


def test_total_deadline():
    async def handler(request):
        await asyncio.sleep(1)
        return response()
    gateway = CloudGateway(config(timeout_seconds=0.01), transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayError, match='timeout'):
        run(gateway)


def test_call_budget_and_input_limit():
    gateway = CloudGateway(config(), transport=httpx.MockTransport(lambda r: response()))
    async def scenario():
        with pytest.raises(GatewayError, match='input_too_large'):
            await gateway.generate({'text': 'x' * 20000}, Answer)
        assert gateway.calls == 0
        for _ in range(4):
            await gateway.generate({}, Answer)
        with pytest.raises(GatewayError, match='call_budget_exceeded'):
            await gateway.generate({}, Answer)
    asyncio.run(scenario())
    assert gateway.calls == 4


def test_cancellation_propagates():
    async def handler(request):
        raise asyncio.CancelledError()
    gateway = CloudGateway(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(asyncio.CancelledError):
        run(gateway)


@pytest.mark.parametrize('mode', ['json_schema', 'none'])
def test_explicit_provider_capabilities(mode):
    def handler(request):
        body = json.loads(request.content)
        assert body['max_completion_tokens'] == 1000
        assert 'max_tokens' not in body
        if mode == 'none':
            assert 'response_format' not in body
        else:
            assert body['response_format']['type'] == 'json_schema'
        return response()
    run(CloudGateway(config(response_mode=mode, output_token_field='max_completion_tokens'), transport=httpx.MockTransport(handler)))


def test_oversized_response():
    gateway = CloudGateway(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * 300000)))
    with pytest.raises(GatewayError, match='response_too_large'):
        run(gateway)


def test_deepseek_flash_preset_and_cache_usage():
    cfg = CloudConfig.deepseek_from_env({'WDA_LLM_API_KEY': 'test-secret'})
    assert cfg.model == 'deepseek-flash'
    assert cfg.base_url == 'https://api.deepseek.com'
    def handler(request):
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        body = json.loads(request.content)
        assert body['thinking'] == {'type': 'disabled'}
        assert body['response_format'] == {'type': 'json_object'}
        return response(usage={'prompt_tokens': 20, 'completion_tokens': 5,
                               'prompt_cache_hit_tokens': 8, 'prompt_cache_miss_tokens': 12})
    result = run(CloudGateway(cfg, transport=httpx.MockTransport(handler)))
    assert result.usage.cache_hit_tokens == 8
    assert result.usage.cache_miss_tokens == 12


def test_generic_config_does_not_send_thinking():
    def handler(request):
        assert 'thinking' not in json.loads(request.content)
        return response()
    run(CloudGateway(config(), transport=httpx.MockTransport(handler)))


def test_invalid_output_still_records_billed_tokens():
    gateway = CloudGateway(config(), transport=httpx.MockTransport(
        lambda r: response('{', usage={'prompt_tokens': 20, 'completion_tokens': 5})))
    with pytest.raises(GatewayError):
        run(gateway)
    assert gateway.usage_history[0].output_tokens == 5


def test_standard_deepseek_key_and_explicit_override():
    assert CloudConfig.deepseek_from_env({'DEEPSEEK_API_KEY': 'standard'}).api_key == 'standard'
    assert CloudConfig.deepseek_from_env({'DEEPSEEK_API_KEY': 'standard', 'WDA_LLM_API_KEY': 'override'}).api_key == 'override'
