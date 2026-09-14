"""M5.4-A: read-only trace projection and its read-only API.

The projection is a pure function over already-persisted evidence. These tests
pin determinism, purity (inputs are never mutated), fail-closed degradation for
missing or malformed evidence, the running / no-event / failed paths, the
read-only API contract, and the frozen invariants this layer must not disturb
(``ARTIFACT_NAMES``, the M4.4 repair boundary, the benchmark and corpus hashes).

The layer adds no model call, no execution, no write and no task artifact.
"""

import ast
import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from benchmarks import HASH_PATH, content_sha256
from backend.app.llm.config import CloudConfig
from backend.app.main import INLINE_ARTIFACTS, create_app
from backend.app.pipeline.repair import APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS
from backend.app.storage.repository import ARTIFACT_NAMES
from backend.app.trace import projection

FROZEN_BENCHMARK_SHA256 = '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'
FROZEN_CASES_SHA256 = '678f87ece8bdb0aa4171f5f80b6b289cd773215639b7e30d82dc4953f5cba1f2'
CASES_PATH = Path(__file__).resolve().parents[1] / 'backend' / 'app' / 'rag' / 'cases.json'

SPEC = {'url': 'http://127.0.0.1:8000/', 'fields': ['id', 'name', 'price_fen'],
        'max_pages': 3, 'click_text': '下一页'}
SUCCESS_FILES = {'evidence.json', 'retrieval.json', 'plan.json', 'result.json', 'report.json',
                 'report.md'}

TRACE_KEYS = {'trace_schema_version', 'task_id', 'status', 'phase', 'source', 'model', 'model_calls',
              'elapsed_seconds', 'timeline_available', 'created_at', 'steps', 'outcome', 'repair',
              'diagnostics', 'artifacts'}
STEP_KEYS = {'key', 'state', 'at', 'duration_s', 'evidence'}
OUTCOME_KEYS = {'status', 'count', 'pages', 'completeness', 'expected_total', 'error', 'error_type',
                'error_category', 'error_repairable'}
REPAIR_KEYS = {'attempted', 'count', 'outcome', 'applied', 'document'}
STAGES = ['observing', 'analyzing', 'executing', 'verifying']

TASK = {'id': '11111111-1111-1111-1111-111111111111', 'status': 'succeeded', 'model': 'fake-model',
        'created_at': '2026-01-01T00:00:00+00:00', 'updated_at': '2026-01-01T00:00:10+00:00'}
EVENTS = [{'seq': 1, 'status': 'created', 'created_at': '2026-01-01T00:00:00+00:00'},
          {'seq': 2, 'status': 'observing', 'created_at': '2026-01-01T00:00:01+00:00'},
          {'seq': 3, 'status': 'analyzing', 'created_at': '2026-01-01T00:00:03+00:00'},
          {'seq': 4, 'status': 'executing', 'created_at': '2026-01-01T00:00:05+00:00'},
          {'seq': 5, 'status': 'verifying', 'created_at': '2026-01-01T00:00:09+00:00'},
          {'seq': 6, 'status': 'succeeded', 'created_at': '2026-01-01T00:00:10+00:00'}]
REPORT = {'task_id': TASK['id'], 'status': 'succeeded', 'model': 'fake-model', 'source': 'browser',
          'count': 30, 'pages': 3, 'completeness': 'complete', 'expected_total': 30,
          'model_calls': 1, 'elapsed_seconds': 4.61}
ARTIFACTS = {
    'report.json': REPORT,
    'evidence.json': [{'request_id': 'req_001'}, {'request_id': 'req_002'}],
    'retrieval.json': {'enabled': True, 'algorithm': 'BM25Okapi', 'corpus_version': 1,
                       'corpus_sha256': 'a' * 64, 'cases': [{'id': 'get_pager'}],
                       'gate': {'applied': True, 'filtered': [],
                                'matched': ['get_pager'], 'conflicted': []}},
    'plan.json': {'fields': ['id', 'name', 'price_fen']},
    'result.json': [{'id': number} for number in range(1, 31)],
}
FAILED_REPORT = {**REPORT, 'status': 'failed', 'error': 'pointer_not_found',
                 'error_type': 'PipelineError', 'error_category': 'PLAN_SEMANTIC',
                 'error_repairable': True, 'error_retryable': False,
                 'repair_attempted': True, 'repair_count': 1, 'repair_outcome': 'applied',
                 'repair_applied': True}
REPAIR_DOCUMENT = {'schema_version': 1, 'attempts': [{'error_code': 'pointer_not_found'}],
                   'proposals': [{'candidate_plan_hash': 'y'}], 'original_plan_hash': 'x',
                   'candidate_plan_hash': 'y', 'applied': True,
                   'execution_result': {'status': 'succeeded'}}
FAILURE_DOCUMENT = {'schema_version': 1,
                    'failure_context': {'error_code': 'pointer_not_found',
                                        'category': 'PLAN_SEMANTIC', 'phase': 'analyzing'},
                    'failure_phase': 'planning',
                    'knowledge_gate': {'applied': True, 'filtered': [], 'matched': [],
                                       'conflicted': []},
                    'case_ids': ['get_pager'], 'corpus_sha256': 'a' * 64}


# --- projection: purity and determinism ---------------------------------

def test_projection_is_pure_and_deterministic():
    task, events, artifacts = copy.deepcopy(TASK), copy.deepcopy(EVENTS), copy.deepcopy(ARTIFACTS)
    first = projection.project_trace(task, events, artifacts)
    second = projection.project_trace(task, events, artifacts)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # 输入不得被修改，事件顺序不影响结果（按 seq 排序）。
    assert (task, events, artifacts) == (TASK, EVENTS, ARTIFACTS)
    assert projection.project_trace(task, list(reversed(events)), artifacts) == first


def test_success_trace_projects_every_stage_from_evidence():
    trace = projection.project_trace(TASK, EVENTS, ARTIFACTS)
    assert set(trace) == TRACE_KEYS
    assert trace['trace_schema_version'] == projection.TRACE_SCHEMA_VERSION == 1
    assert trace['task_id'] == TASK['id'] and trace['status'] == 'succeeded'
    assert trace['phase'] == 'terminal' and trace['timeline_available'] is True
    assert trace['source'] == 'browser' and trace['model'] == 'fake-model'
    assert trace['model_calls'] == 1 and trace['elapsed_seconds'] == 4.61
    assert trace['artifacts'] == [] and trace['diagnostics'] == {'failure_retrieval': None}

    assert [step['key'] for step in trace['steps']] == STAGES
    assert all(set(step) == STEP_KEYS for step in trace['steps'])
    assert [step['state'] for step in trace['steps']] == ['reached'] * 4
    observing, analyzing, executing, verifying = trace['steps']
    assert observing['evidence'] == {'source': 'browser', 'observations': 2}
    assert analyzing['evidence']['model_calls'] == 1
    assert analyzing['evidence']['plan_fields'] == ['id', 'name', 'price_fen']
    assert analyzing['evidence']['retrieval']['gate_applied'] is True
    assert analyzing['evidence']['retrieval']['case_ids'] == ['get_pager']
    assert executing['evidence']['count'] == 30
    assert verifying['evidence'] == {'completeness': 'complete', 'expected_total': 30}
    assert verifying['duration_s'] == 4.0

    assert set(trace['outcome']) == OUTCOME_KEYS
    assert trace['outcome'] == {'status': 'succeeded', 'count': 30, 'pages': 3,
                                'completeness': 'complete', 'expected_total': 30, 'error': None,
                                'error_type': None, 'error_category': None,
                                'error_repairable': None}
    assert set(trace['repair']) == REPAIR_KEYS
    assert trace['repair'] == {'attempted': None, 'count': None, 'outcome': None,
                               'applied': None, 'document': None}


def test_running_task_shows_reached_and_pending():
    running = {**TASK, 'status': 'observing'}
    trace = projection.project_trace(running, EVENTS[:2], {})
    assert trace['phase'] == 'running' and trace['timeline_available'] is True
    assert [step['state'] for step in trace['steps']] == ['reached', 'pending', 'pending', 'pending']
    # 已到达阶段给出显式 null，未到达阶段没有证据对象。
    assert trace['steps'][0]['evidence'] == {'source': None, 'observations': None}
    assert all(step['evidence'] is None for step in trace['steps'][1:])
    assert set(trace['outcome'].values()) == {None}


def test_failure_trace_projects_repair_and_diagnostic_evidence():
    failed = {**TASK, 'status': 'failed'}
    events = EVENTS[:4] + [{'seq': 5, 'status': 'failed',
                            'created_at': '2026-01-01T00:00:06+00:00'}]
    artifacts = {'report.json': FAILED_REPORT, 'repair.json': REPAIR_DOCUMENT,
                 'failure_retrieval.json': FAILURE_DOCUMENT}
    trace = projection.project_trace(failed, events, artifacts)
    assert [step['state'] for step in trace['steps']] == ['reached', 'reached', 'failed', 'absent']
    assert trace['outcome']['error'] == 'pointer_not_found'
    assert trace['outcome']['error_category'] == 'PLAN_SEMANTIC'
    assert trace['outcome']['error_repairable'] is True
    assert trace['repair'] == {'attempted': True, 'count': 1, 'outcome': 'applied', 'applied': True,
                               'document': {'attempts': 1, 'proposals': 1}}
    diagnostic = trace['diagnostics']['failure_retrieval']
    assert diagnostic['failure_phase'] == 'planning'
    assert diagnostic['failure_context'] == {'error_code': 'pointer_not_found',
                                             'category': 'PLAN_SEMANTIC', 'phase': 'analyzing'}
    assert diagnostic['case_ids'] == ['get_pager']
    assert diagnostic['knowledge_gate_applied'] is True


def test_missing_evidence_degrades_to_absent_and_null():
    events = EVENTS[:2] + [{'seq': 3, 'status': 'failed',
                            'created_at': '2026-01-01T00:00:02+00:00'}]
    trace = projection.project_trace({**TASK, 'status': 'failed'}, events, {})
    assert [step['state'] for step in trace['steps']] == ['failed', 'absent', 'absent', 'absent']
    assert trace['steps'][0]['evidence'] == {'source': None, 'observations': None}
    assert all(step['evidence'] is None for step in trace['steps'][1:])
    assert set(trace['outcome'].values()) == {None}
    assert trace['repair'] == {'attempted': None, 'count': None, 'outcome': None,
                               'applied': None, 'document': None}
    assert trace['diagnostics'] == {'failure_retrieval': None}
    assert trace['artifacts'] == []
    assert trace['model_calls'] is None and trace['elapsed_seconds'] is None


def test_task_without_events_has_no_fabricated_timeline():
    trace = projection.project_trace(TASK, [], ARTIFACTS)
    assert trace['timeline_available'] is False
    assert [step['state'] for step in trace['steps']] == ['absent'] * 4
    assert trace['steps'][0]['duration_s'] is None
    # 结果仍严格来自产物，不因缺少事件而丢失。
    assert trace['status'] == 'succeeded'
    assert trace['outcome']['count'] == 30


def test_malformed_inputs_fail_closed_without_raising():
    trace = projection.project_trace(None, None, None, None)
    assert set(trace) == TRACE_KEYS
    assert trace['task_id'] is None and trace['status'] is None
    assert trace['phase'] == 'running' and trace['timeline_available'] is False
    assert [step['state'] for step in trace['steps']] == ['pending'] * 4
    assert trace['artifacts'] == [] and trace['diagnostics'] == {'failure_retrieval': None}

    hostile = {'report.json': ['not', 'a', 'mapping'], 'evidence.json': 'text',
               'retrieval.json': {'cases': 'nope', 'gate': 'nope'},
               'plan.json': {'fields': 'nope'},
               'failure_retrieval.json': {'case_ids': [1, 2], 'knowledge_gate': 'nope',
                                          'failure_context': 'nope'},
               'repair.json': {'attempts': 'nope', 'proposals': 'nope'}}
    trace = projection.project_trace({'id': 1, 'status': 3}, [{'seq': 'x'}, 'nope', None], hostile)
    assert trace['task_id'] is None and trace['status'] is None
    assert all(step['evidence'] is None for step in trace['steps'])
    assert set(trace['outcome'].values()) == {None}
    assert trace['repair']['document'] is None
    diagnostic = trace['diagnostics']['failure_retrieval']
    assert diagnostic['case_ids'] == []
    assert diagnostic['failure_context'] is None
    assert diagnostic['knowledge_gate_applied'] is None


def test_projection_has_no_execution_provider_or_io_imports():
    source = Path(projection.__file__).read_text(encoding='utf-8')
    for forbidden in ('execute_plan', 'subprocess', 'RepairProposer', 'run_task', 'gateway',
                      'openai', 'httpx', 'requests', 'urllib', 'socket', 'json.load'):
        assert forbidden not in source, forbidden
    modules = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append('.' * node.level + (node.module or ''))
    assert modules == ['collections.abc', 'datetime', '..storage.repository']
    assert sorted(projection.TRACE_STATES) == ['absent', 'failed', 'pending', 'reached']


# --- read-only API ------------------------------------------------------

def config():
    return CloudConfig('https://example.test', 'not-a-real-secret', 'fake-model')


async def trace_worker(spec, cfg, task_id, root, stage):
    for name in STAGES:
        await stage(name)
    folder = root / 'tasks' / task_id
    folder.mkdir(parents=True, exist_ok=True)
    artifacts = {'evidence.json': [{'request_id': 'req_001'}],
                 'retrieval.json': {'enabled': True, 'algorithm': 'BM25Okapi', 'cases': [],
                                    'gate': {'applied': False, 'filtered': [], 'matched': [],
                                             'conflicted': []}},
                 'plan.json': {'fields': ['id']}, 'result.json': [{'id': 1}],
                 'report.json': {'status': 'succeeded', 'count': 1, 'model_calls': 1,
                                 'completeness': 'complete', 'elapsed_seconds': 0.1}}
    for name, value in artifacts.items():
        (folder / name).write_text(json.dumps(value), encoding='utf-8')
    return artifacts['report.json']


def wait_terminal(client, task_id):
    for _ in range(100):
        if client.get(f'/api/v1/tasks/{task_id}').json()['status'] in {
                'succeeded', 'failed', 'cancelled', 'interrupted'}:
            return
        time.sleep(0.01)
    raise AssertionError('task did not finish')


def test_trace_endpoint_projects_a_terminal_task(tmp_path):
    with TestClient(create_app(tmp_path, config, trace_worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        wait_terminal(client, task_id)
        trace = client.get(f'/api/v1/tasks/{task_id}/trace').json()
        assert trace['task_id'] == task_id and trace['status'] == 'succeeded'
        assert trace['phase'] == 'terminal' and trace['model_calls'] == 1
        assert [step['state'] for step in trace['steps']] == ['reached'] * 4
        assert {entry['filename'] for entry in trace['artifacts']} == SUCCESS_FILES
        assert all(len(entry['sha256']) == 64 for entry in trace['artifacts'])
        assert client.get('/api/v1/tasks/not-found/trace').status_code == 404


def test_trace_endpoint_reports_running_task_without_artifacts(tmp_path):
    async def slow_worker(spec, cfg, task_id, root, stage):
        await stage('observing')
        await asyncio.sleep(30)
    with TestClient(create_app(tmp_path, config, slow_worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        for _ in range(100):
            if client.get(f'/api/v1/tasks/{task_id}').json()['status'] == 'observing':
                break
            time.sleep(0.01)
        trace = client.get(f'/api/v1/tasks/{task_id}/trace').json()
        assert trace['phase'] == 'running'
        assert [step['state'] for step in trace['steps']] == ['reached', 'pending', 'pending', 'pending']
        assert trace['artifacts'] == []
        assert client.get(f'/api/v1/tasks/{task_id}/artifacts').json()['items'] == []
        client.post(f'/api/v1/tasks/{task_id}/cancel')


def test_inline_artifact_is_allowlisted_and_integrity_checked(tmp_path):
    with TestClient(create_app(tmp_path, config, trace_worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        wait_terminal(client, task_id)
        base = f'/api/v1/tasks/{task_id}/artifacts'
        assert client.get(f'{base}/result.json').json() == [{'id': 1}]
        assert client.get(f'{base}/report.json').json()['status'] == 'succeeded'
        assert client.get(f'{base}/result.json').headers['content-type'].startswith('application/json')
        # 非 JSON 与未知文件名都不内联。
        assert client.get(f'{base}/collector.py').status_code == 404
        assert client.get(f'{base}/report.md').status_code == 404
        assert client.get(f'{base}/missing.json').status_code == 404
        assert client.get(f'{base}/..%2Freport.json').status_code == 404
        assert client.get('/api/v1/tasks/not-found/artifacts/report.json').status_code == 404
        # 内容被改动时沿用既有 sha256 校验语义，不返回被篡改的内容。
        (tmp_path / 'tasks' / task_id / 'result.json').write_text('{"tampered":true}', encoding='utf-8')
        assert client.get(f'{base}/result.json').status_code == 409
        trace = client.get(f'/api/v1/tasks/{task_id}/trace').json()
        assert trace['steps'][2]['evidence']['count'] == 1      # 降级为报告中的条数


def test_trace_layer_does_not_disturb_frozen_contracts():
    expected = {'evidence.json', 'cloud_payload.json', 'plan.json', 'result.json', 'report.json',
                'report.md', 'retrieval.json', 'collector.py', 'collector_result.json',
                'collector_verification.json', 'repair.json', 'failure_retrieval.json'}
    assert ARTIFACT_NAMES == expected
    assert APPLICABLE_ERROR_CODES == frozenset({'pointer_not_found'})
    assert MAX_REPAIR_ATTEMPTS == 1
    assert content_sha256() == FROZEN_BENCHMARK_SHA256
    assert HASH_PATH.read_text(encoding='utf-8').strip() == FROZEN_BENCHMARK_SHA256
    assert hashlib.sha256(CASES_PATH.read_bytes()).hexdigest() == FROZEN_CASES_SHA256
    # 内联白名单只能是已注册 JSON 产物的子集，绝不能引入可执行或富文本文件。
    assert INLINE_ARTIFACTS <= ARTIFACT_NAMES
    assert all(name.endswith('.json') for name in INLINE_ARTIFACTS)
    assert 'collector.py' not in INLINE_ARTIFACTS and 'report.md' not in INLINE_ARTIFACTS
