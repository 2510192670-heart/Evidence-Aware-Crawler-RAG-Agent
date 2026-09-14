"""M4.4.1 repair foundation: contract, plan hash, fail-closed policy and audit.

Repair is a decision, not an action, in this milestone: the pipeline may only
classify a failure, check eligibility and record an audit. These tests lock the
contract, the fail-closed defaults and the guarantee that a task which never
enters a repair path behaves exactly as before.
"""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.llm.config import CloudConfig
from backend.app.main import create_app
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation, plan_hash
from backend.app.pipeline.errors import PipelineError
from backend.app.pipeline.repair import (
    MAX_REPAIR_ATTEMPTS,
    RepairAttempt,
    RepairContext,
    RepairPolicyChecker,
    RepairReason,
    audit_document,
    build_attempt,
)

SPEC = {'url': 'http://127.0.0.1:8000/', 'fields': ['id', 'name', 'price_fen'],
        'max_pages': 3, 'click_text': '下一页'}
HASH = 'a' * 64


def plan(**overrides):
    values = dict(request_id='req_001', items_pointer='/items',
                  fields={'id': '/id', 'name': '/name'}, unique_key='id',
                  page_parameter='page', has_next_pointer='/has_next', total_pointer='/total')
    values.update(overrides)
    return ExtractionPlan(**values)


def config():
    return CloudConfig('https://example.test', 'not-a-real-secret', 'fake-model')


# --- 1. RepairAttempt contract & serialization ---------------------------

def test_repair_attempt_serializes_to_a_stable_program_generated_shape():
    attempt = build_attempt(PipelineError('pointer_not_found', details={'pointer': '/missing'}),
                            RepairContext(original_plan_hash=plan_hash(plan())))
    assert attempt.to_dict() == {
        'attempt': 1,
        'error_code': 'pointer_not_found',
        'error_category': 'PLAN_SEMANTIC',
        'failure_phase': 'analyzing',
        'collection_started': False,
        'original_plan_hash': plan_hash(plan()),
        'candidate_plan_hash': None,
        'modified_fields': [],
        'reason_code': 'repairable',
        'validation_result': None,
        'rejection_code': None,
        'outcome': 'deferred',
    }
    document = audit_document([attempt])
    assert document['schema_version'] == 1
    assert json.loads(json.dumps(document, allow_nan=False)) == document


def test_repair_attempt_refuses_values_outside_the_registry():
    base = dict(attempt=1, error_code='x', error_category='PLAN_SEMANTIC', failure_phase='analyzing',
                collection_started=False, original_plan_hash=None, candidate_plan_hash=None,
                modified_fields=(), reason_code='repairable', validation_result=None,
                rejection_code=None, outcome='deferred')
    with pytest.raises(ValueError):
        RepairAttempt(**{**base, 'outcome': 'made_up'})
    with pytest.raises(ValueError):
        RepairAttempt(**{**base, 'reason_code': 'made_up'})
    with pytest.raises(ValueError):
        RepairAttempt(**{**base, 'error_category': 'MADE_UP'})
    with pytest.raises(ValueError):
        RepairAttempt(**{**base, 'attempt': 0})
    # 恰好一个决定码：允许给理由、拒绝给拒绝码，二者皆空或皆有都是编程错误。
    with pytest.raises(ValueError):
        RepairAttempt(**{**base, 'reason_code': None, 'rejection_code': None})
    with pytest.raises(ValueError):
        RepairAttempt(**{**base, 'rejection_code': 'not_repairable'})


# --- 2 / 3. canonical plan hash -----------------------------------------

def test_plan_hash_is_stable_for_the_same_plan():
    digest = plan_hash(plan())
    assert digest == plan_hash(plan())
    assert len(digest) == 64 and set(digest) <= set('0123456789abcdef')
    # fields 字典的插入顺序不影响哈希。
    reordered = plan(fields={'name': '/name', 'id': '/id'})
    assert plan_hash(reordered) == plan_hash(plan())


def test_plan_hash_changes_when_any_field_changes():
    original = plan()
    for update in ({'items_pointer': '/rows'}, {'request_id': 'req_002'},
                   {'page_parameter': 'p'}, {'unique_key': 'name'},
                   {'total_pointer': None}, {'has_next_pointer': None},
                   {'fields': {'id': '/id', 'name': '/title'}},
                   {'pagination_location': 'json_body'}):
        assert plan_hash(original.model_copy(update=update)) != plan_hash(original), update


# --- 4 / 5 / 6. fail-closed eligibility ---------------------------------

def test_security_errors_are_rejected():
    error = PipelineError('sensitive_pointer', details={'pointer': '/token'})
    decision = RepairPolicyChecker().check(error, RepairContext(original_plan_hash=HASH))
    assert not decision.eligible
    assert decision.reason is RepairReason.NOT_REPAIRABLE
    attempt = build_attempt(error, RepairContext(original_plan_hash=HASH))
    assert attempt.outcome == 'rejected'
    assert attempt.reason_code is None
    assert attempt.rejection_code == 'not_repairable'


def test_data_integrity_errors_are_rejected():
    error = PipelineError('duplicate_id', details={'page': 2, 'unique_key': 'id'})
    attempt = build_attempt(error, RepairContext(original_plan_hash=HASH))
    assert attempt.error_category == 'DATA_INTEGRITY'
    assert attempt.failure_phase == 'executing'
    assert attempt.collection_started is True
    assert attempt.outcome == 'rejected'
    assert attempt.rejection_code == 'not_repairable'


def test_unclassified_errors_are_rejected_and_never_enable_collection_repair():
    attempt = build_attempt(RuntimeError('duplicate_id'), RepairContext(original_plan_hash=HASH))
    assert attempt.error_code == 'RuntimeError'
    assert attempt.error_category == 'UNCLASSIFIED'
    assert attempt.failure_phase == 'unknown'
    # 无法证明采集尚未开始时按已开始处理（fail closed）。
    assert attempt.collection_started is True
    assert attempt.outcome == 'rejected'
    assert attempt.rejection_code == 'unclassified'


def test_repair_policy_is_bounded_and_requires_the_original_plan():
    error = PipelineError('pointer_not_found', details={'pointer': '/missing'})
    checker = RepairPolicyChecker()
    assert checker.check(error).reason is RepairReason.MISSING_PLAN
    assert checker.check(error, RepairContext(original_plan_hash=HASH)).reason is RepairReason.REPAIRABLE
    spent = RepairContext(original_plan_hash=HASH, attempts=MAX_REPAIR_ATTEMPTS)
    assert checker.check(error, spent).reason is RepairReason.BUDGET_EXHAUSTED
    assert not checker.check(error, spent).eligible


# --- 7. report & artifact compatibility ---------------------------------
#
# 真实任务路径：失败走 run_task 的异常分支，成功不进入任何 repair 路径。

def run_pipeline(tmp_path, monkeypatch, error=None):
    class Gateway:
        calls = 0
        usage_history = []

        def __init__(self, configuration):
            pass

        async def generate(self, payload, schema):
            self.calls += 1
            return SimpleNamespace(data=ExtractionPlan(
                request_id='req_001', items_pointer='/items', fields={'id': '/id'}, unique_key='id',
                page_parameter='page', has_next_pointer=None, total_pointer=None))

    async def observe(*args):
        return [Observation(request_id='req_001', url='http://127.0.0.1:8100/catalog/v1/items',
                            query={'page': '1'}, body={'items': [{'id': 1}]})]

    async def fake_execute(*args, **kwargs):
        if error is not None:
            raise error
        return {'items': [{'id': 1}], 'pages': 1, 'expected_total': None,
                'completeness': 'stop_condition_only'}

    async def fake_export(*args, **kwargs):
        return {'collector_exported': True, 'collector_execution_success': True,
                'collector_matches_internal_result': True}

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(run, 'execute_plan', fake_execute)
    monkeypatch.setattr(run, 'export_collector', fake_export)
    args = SimpleNamespace(url='http://127.0.0.1:8100/', click_text='', fields='id',
                           max_pages=1, rag_enabled=False)
    asyncio.run(run.run_task(args, SimpleNamespace(model='fake-model'), output_root=tmp_path, quiet=True))
    folder = next((tmp_path / 'tasks').iterdir())
    return json.loads((folder / 'report.json').read_text(encoding='utf-8')), folder


def test_successful_task_has_no_repair_path_and_one_model_call(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch)
    assert report['status'] == 'succeeded'
    assert report['count'] == 1
    # 旧字段保持兼容，新增字段只有三个且为未尝试状态。
    assert {'task_id', 'status', 'model', 'source', 'retrieval', 'count', 'pages',
            'completeness', 'elapsed_seconds', 'model_calls', 'usage'} <= set(report)
    assert report['repair_attempted'] is False
    assert report['repair_count'] == 0
    assert report['repair_outcome'] is None
    # 没有修复路径就不会有第二次模型调用，也不会有审计产物。
    assert report['model_calls'] == 1
    assert not (folder / 'repair.json').exists()


def test_failed_task_audits_the_rejection_without_repairing(tmp_path, monkeypatch):
    report, folder = run_pipeline(
        tmp_path, monkeypatch, error=PipelineError('duplicate_id', details={'page': 2, 'unique_key': 'id'}))
    assert report['status'] == 'failed'
    assert report['error'] == 'duplicate_id'
    assert report['repair_attempted'] is False
    assert report['repair_count'] == 0
    assert report['repair_outcome'] == 'rejected'
    attempt = json.loads((folder / 'repair.json').read_text(encoding='utf-8'))['attempts'][0]
    assert attempt['error_code'] == 'duplicate_id'
    assert attempt['outcome'] == 'rejected'
    assert attempt['candidate_plan_hash'] is None and attempt['modified_fields'] == []
    assert attempt['original_plan_hash'] == plan_hash(ExtractionPlan(
        request_id='req_001', items_pointer='/items', fields={'id': '/id'}, unique_key='id',
        page_parameter='page', has_next_pointer=None, total_pointer=None))


async def legacy_worker(spec, cfg, task_id, root, stage):
    folder = root / 'tasks' / task_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'result.json').write_text('[{"id":1}]', encoding='utf-8')
    report = {'status': 'succeeded', 'count': 1, 'model_calls': 0}
    (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    return report


def repair_worker(spec, cfg, task_id, root, stage):
    folder = root / 'tasks' / task_id
    folder.mkdir(parents=True, exist_ok=True)
    attempt = build_attempt(PipelineError('duplicate_id', details={'page': 2, 'unique_key': 'id'}),
                            RepairContext(original_plan_hash=HASH))
    (folder / 'repair.json').write_text(json.dumps(audit_document([attempt])), encoding='utf-8')
    report = {'status': 'failed', 'error': 'duplicate_id', 'repair_attempted': False,
              'repair_count': 0, 'repair_outcome': attempt.outcome}
    (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    return report


def wait_terminal(client, task_id):
    import time
    for _ in range(100):
        task = client.get(f'/api/v1/tasks/{task_id}').json()
        if task['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
            return task
        time.sleep(0.01)
    pytest.fail('Task did not finish')


def test_legacy_task_without_a_repair_artifact_is_untouched(tmp_path):
    with TestClient(create_app(tmp_path, config, legacy_worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        task = wait_terminal(client, task_id)
        assert task['status'] == 'succeeded'
        # 旧报告不会被注入新字段；缺失 repair.json 也不会影响产物登记。
        assert set(task['summary']) == {'status', 'count', 'model_calls'}
        names = {a['filename'] for a in client.get(f'/api/v1/tasks/{task_id}/artifacts').json()['items']}
        assert names == {'report.json', 'report.md', 'result.json'}


def test_repair_artifact_is_registered_and_downloadable(tmp_path):
    with TestClient(create_app(tmp_path, config, repair_worker)) as client:
        task_id = client.post('/api/v1/tasks', json=SPEC).json()['id']
        wait_terminal(client, task_id)
        entry = next(a for a in client.get(f'/api/v1/tasks/{task_id}/artifacts').json()['items']
                     if a['filename'] == 'repair.json')
        download = client.get(f'/api/v1/artifacts/{entry["id"]}/download')
        assert download.status_code == 200
        assert download.headers['content-type'].startswith('application/json')
        assert hashlib.sha256(download.content).hexdigest() == entry['sha256']
        payload = download.json()
        assert payload['schema_version'] == 1
        assert payload['attempts'][0]['error_code'] == 'duplicate_id'
        assert payload['attempts'][0]['outcome'] == 'rejected'
