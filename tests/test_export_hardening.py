import asyncio
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend.app.pipeline.contracts import ExtractionPlan, Observation, has_sensitive_keys
from backend.app.pipeline.execution import execute_plan
from backend.app.pipeline.export import build_collector_source, compare_exported_result, export_collector
from backend.app.pipeline.observe import request_json_object


def nested(depth, leaf):
    for _ in range(depth):
        leaf = {'nested': leaf}
    return leaf


def evidence(url='http://127.0.0.1:1/search', method='POST', payload=None):
    return Observation(request_id='r', url=url, method=method,
                       query={} if method == 'POST' else {'page': '1'},
                       request_body=payload if payload is not None else ({'page': 1} if method == 'POST' else None),
                       body={'items': [{'id': 1}], 'total': 2, 'more': True})


def plan(method='POST', **changes):
    values = dict(request_id='r', items_pointer='/items', fields={'id': '/id'}, unique_key='id',
                  pagination_location='json_body' if method == 'POST' else 'query',
                  page_parameter='page', has_next_pointer='/more', total_pointer='/total')
    return ExtractionPlan(**(values | changes))


@pytest.mark.parametrize('depth', [0, 5, 6, 7, 12])
@pytest.mark.parametrize('leaf', [{'token': 'private'}, [{'api_key': 'private'}]])
def test_depth_cannot_hide_sensitive_keys(depth, leaf, tmp_path):
    payload = {'page': 1, **nested(depth, leaf if isinstance(leaf, dict) else {'rows': leaf})}
    assert has_sensitive_keys(payload)
    assert request_json_object({'Content-Type': 'application/json'}, json.dumps(payload)) is None
    with pytest.raises(ValueError, match='sensitive_request_not_supported'):
        asyncio.run(export_collector(tmp_path, plan(), evidence(payload=payload), {}, 2))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('depth,blocked', [(5, False), (6, False), (7, True), (12, True)])
def test_scan_depth_boundary_for_unknown_containers(depth, blocked):
    assert has_sensitive_keys(nested(depth, {})) is blocked


def test_execution_rejects_unscannable_body_before_network():
    async def scenario():
        def unexpected(request):
            pytest.fail('unsafe body reached network')
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            with pytest.raises(ValueError, match='sensitive_request_not_supported'):
                await execute_plan(client, plan(), [evidence(payload={'page': 1, 'filter': nested(8, {})})], 2)
    asyncio.run(scenario())


@pytest.fixture
def replay_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else None
            requests.append((self.command, urlsplit(self.path).path, query, payload))
            page = payload['page'] if payload is not None else int(query['page'][0])
            raw = json.dumps({'items': [{'id': page}], 'total': 2, 'more': page < 2}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/search', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('missing_pointer', ['has_next_pointer', 'total_pointer'])
def test_config_literals_compile_and_run(method, missing_pointer, replay_server, tmp_path):
    url, requests = replay_server
    payload = {'page': 1, 'filter': {'optional': None, 'enabled': True, 'disabled': False,
                                   'text': '', 'rows': [], 'object': {}, 'quoted': "a'\\b\n中文"}}
    observation = evidence(url, method, payload if method == 'POST' else None)
    extraction = plan(method, **{missing_pointer: None})
    source = build_collector_source(extraction, observation, 2)
    compile(source, 'collector.py', 'exec')
    assert source == build_collector_source(extraction, observation, 2)

    async def scenario():
        async with httpx.AsyncClient(trust_env=False) as client:
            internal = await execute_plan(client, extraction, [observation], 2)
        return await export_collector(tmp_path, extraction, observation, internal, 2)

    result = asyncio.run(scenario())
    assert result['collector_execution_success']
    assert result['collector_matches_internal_result']
    assert len(requests) == 4
    assert requests[:2] == requests[2:]
    if method == 'POST':
        assert requests[2][3] == payload
        assert requests[3][3] == payload | {'page': 2}


def test_post_query_replay_matches_internal(replay_server, tmp_path):
    url, requests = replay_server
    observation = evidence(url)
    observation.query = {'lang': 'zh', 'empty': '', 'q': '中文 + &'}

    async def scenario():
        async with httpx.AsyncClient(trust_env=False) as client:
            internal = await execute_plan(client, plan(), [observation], 2)
        return await export_collector(tmp_path, plan(), observation, internal, 2)

    result = asyncio.run(scenario())
    assert result['collector_matches_internal_result']
    assert len(requests) == 4
    for page, request in zip([1, 2, 1, 2], requests):
        assert request == ('POST', '/search', {'lang': ['zh'], 'empty': [''], 'q': ['中文 + &']}, {'page': page})


@pytest.mark.parametrize('change', [
    {'pages': 9}, {'completeness': 'partial'}, {'items': [{'id': 2, 'name': 'a'}]},
    {'items': [{'id': 1, 'other': 'a'}]}, {'items': [{'id': 1, 'name': 1}]},
    {'items': [{'id': 1, 'name': 'a'}, {'id': 1, 'name': 'a'}]},
])
def test_verification_rejects_mismatched_contract(change):
    internal = {'items': [{'id': 1, 'name': 'a'}], 'pages': 1, 'expected_total': 1, 'completeness': 'complete'}
    exported = copy.deepcopy(internal) | change
    assert not compare_exported_result(internal, exported, 'id')['matches']
