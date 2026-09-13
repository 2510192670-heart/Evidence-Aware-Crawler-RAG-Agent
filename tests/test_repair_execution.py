"""M4.4.3 bounded execution repair: the first time a candidate may affect execution.

Repair is now an action, so these tests pin the safety envelope: only the
explicit apply allowlist can execute, the original plan is never mutated or
overwritten, the candidate must pass the executor's own validator first, the
extra execution is bounded to one, forbidden categories never execute, and the
exported collector still matches the (repaired) internal result.
"""

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from backend.app.llm.gateway import GatewayError
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation, plan_hash
from backend.app.pipeline.errors import PipelineError
from backend.app.pipeline.repair import (
    APPLICABLE_ERROR_CODES,
    ProposalReason,
    RepairApplication,
    RepairProposer,
    audit_document,
    is_applicable,
)


def record(**overrides):
    values = dict(request_id='req_001', url='http://127.0.0.1:8100/catalog/v1/items',
                  query={'page': '1'}, body={'items': [{'id': 1}], 'total': 1, 'has_next': False})
    values.update(overrides)
    return Observation(**values)


def plan(**overrides):
    values = dict(request_id='req_001', items_pointer='/missing', fields={'id': '/id'},
                  unique_key='id', page_parameter='page', has_next_pointer=None, total_pointer=None)
    values.update(overrides)
    return ExtractionPlan(**values)


def run_broken_task(tmp_path, monkeypatch, *, execute, proposer=None):
    """Drive run_task with a fixed model plan and a controlled executor."""
    original = plan()

    class Gateway:
        calls = 0
        usage_history = []

        def __init__(self, configuration):
            pass

        async def generate(self, payload, schema):
            self.calls += 1
            return SimpleNamespace(data=original)

    async def observe(*args):
        return [record()]

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(run, 'execute_plan', execute)
    if proposer is not None:
        monkeypatch.setattr(run, 'RepairProposer', lambda *args, **kwargs: proposer)
    args = SimpleNamespace(url='http://127.0.0.1:8100/', click_text='', fields='id',
                           max_pages=1, rag_enabled=False)
    asyncio.run(run.run_task(args, SimpleNamespace(model='fake'), output_root=tmp_path, quiet=True))
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    return report, folder


# --- unit: allowlist and audit schema ------------------------------------

def test_apply_allowlist_contains_only_pointer_not_found():
    assert APPLICABLE_ERROR_CODES == {'pointer_not_found'}
    proposer = RepairProposer()
    error = PipelineError('pointer_not_found', details={'pointer': '/missing'})
    candidate, proposal = proposer.evaluate(error, plan(), record())
    assert candidate is not None and proposal.outcome == 'proposed'
    assert is_applicable(error, proposal) is True

    mismatch = PipelineError('page_location_mismatch', details={'parameter': 'page'})
    candidate2, proposal2 = proposer.evaluate(
        mismatch, plan(items_pointer='/items', pagination_location='query'),
        record(method='POST', query={}, request_body={'page': 1}, body={'items': [{'id': 1}]}))
    # 可以生成候选，但绝不在自动应用白名单内。
    assert candidate2 is not None and proposal2.outcome == 'proposed'
    assert is_applicable(mismatch, proposal2) is False


def test_candidate_is_a_new_plan_and_the_original_is_untouched():
    original = plan()
    snapshot = original.model_dump()
    candidate, proposal = RepairProposer().evaluate(
        PipelineError('pointer_not_found', details={'pointer': '/missing'}), original, record())
    assert candidate is not original
    assert candidate.items_pointer == '/items'
    assert original.model_dump() == snapshot
    assert original.items_pointer == '/missing'
    assert plan_hash(candidate) == proposal.candidate_plan_hash


def test_repair_application_schema_and_document_extension():
    application = RepairApplication('a' * 64, 'b' * 64, True, {'status': 'succeeded', 'count': 1})
    assert application.to_dict() == {'original_plan_hash': 'a' * 64, 'candidate_plan_hash': 'b' * 64,
                                     'applied': True, 'execution_result': {'status': 'succeeded', 'count': 1}}
    with pytest.raises(ValueError):
        RepairApplication('a' * 64, None, True, None)          # applied 必须有候选哈希
    with pytest.raises(ValueError):
        RepairApplication('a' * 64, 'b' * 64, 'yes', None)     # applied 必须是 bool
    with pytest.raises(ValueError):
        RepairApplication('a' * 64, None, False, ['nope'])
    document = audit_document([], [], application)
    assert {'original_plan_hash', 'candidate_plan_hash', 'applied', 'execution_result'} <= set(document)
    # 未提供 application 时四个字段仍稳定存在（向后兼容）。
    assert audit_document([], [])['applied'] is False


# --- candidate validation is required before execution -------------------

def test_candidate_must_pass_validation_before_any_execution(tmp_path, monkeypatch):
    calls = {'execute': 0}

    def broken_generator(error, candidate_plan, evidence):
        return (candidate_plan.model_copy(update={'items_pointer': '/missing2'}),
                ('items_pointer',), ProposalReason.POINTER_RELOCATED)

    async def failing(*args, **kwargs):
        calls['execute'] += 1
        raise PipelineError('pointer_not_found', details={'pointer': '/missing'})

    report, folder = run_broken_task(tmp_path, monkeypatch, execute=failing,
                                     proposer=RepairProposer({'pointer_not_found': broken_generator}))
    assert calls['execute'] == 1              # 候选校验失败 → 不会产生第二次执行
    assert report['status'] == 'failed'
    assert report['repair_attempted'] is False and report['repair_count'] == 0
    assert report['repair_applied'] is False and report['repair_candidate_plan_hash'] is None
    assert report['repair_outcome'] == 'rejected'
    document = json.loads((folder / 'repair.json').read_text(encoding='utf-8'))
    assert document['applied'] is False and document['candidate_plan_hash'] is None
    assert document['proposals'][0]['outcome'] == 'rejected'
    assert document['proposals'][0]['validation_result'].startswith('invalid:')


# --- 4. the repair budget is real ----------------------------------------

def test_repair_is_bounded_to_one_execution(tmp_path, monkeypatch):
    calls = {'execute': 0}

    async def always_pointer_not_found(*args, **kwargs):
        calls['execute'] += 1
        raise PipelineError('pointer_not_found', details={'pointer': '/missing'})

    report, folder = run_broken_task(tmp_path, monkeypatch, execute=always_pointer_not_found)
    assert calls['execute'] == 2              # 原始一次 + 有界修复一次，绝不无限重试
    assert report['status'] == 'failed'
    assert report['model_calls'] == 1         # 修复不产生第二次模型调用
    assert report['repair_attempted'] is True and report['repair_count'] == 1
    assert report['repair_applied'] is True and report['repair_outcome'] == 'applied_failed'
    document = json.loads((folder / 'repair.json').read_text(encoding='utf-8'))
    assert document['applied'] is True
    assert document['execution_result']['status'] == 'failed'
    assert len(document['attempts']) == 2
    assert document['attempts'][0]['reason_code'] == 'repairable'
    assert document['attempts'][1]['rejection_code'] == 'repair_budget_exhausted'


# --- 5. forbidden categories never execute a repair ----------------------

@pytest.mark.parametrize('error', [
    PipelineError('sensitive_pointer', details={'pointer': '/token'}),
    PipelineError('duplicate_id', details={'page': 2, 'unique_key': 'id'}),
    GatewayError('connect_timeout'),
    RuntimeError('boom'),
], ids=['security', 'data_integrity', 'transport', 'unclassified'])
def test_forbidden_categories_never_execute_repair(tmp_path, monkeypatch, error):
    calls = {'execute': 0}

    async def failing(*args, **kwargs):
        calls['execute'] += 1
        raise error

    report, folder = run_broken_task(tmp_path, monkeypatch, execute=failing)
    assert calls['execute'] == 1
    assert report['status'] == 'failed'
    assert report['repair_attempted'] is False and report['repair_count'] == 0
    assert report['repair_applied'] is False and report['repair_candidate_plan_hash'] is None
    assert report['repair_outcome'] == 'rejected'
    document = json.loads((folder / 'repair.json').read_text(encoding='utf-8'))
    assert document['applied'] is False and document['candidate_plan_hash'] is None
    assert document['proposals'] == []


# --- 1 / 2 / 6 / 7. end to end on a real loopback target -----------------

@pytest.fixture(scope='module')
def shop_server():
    import uvicorn
    from main import app
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail('loopback test server did not start')
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True
    thread.join(timeout=10)


def test_pointer_not_found_repair_succeeds_and_collector_still_verifies(shop_server, tmp_path, monkeypatch):
    url = shop_server + '/api/products'
    with httpx.Client(trust_env=False) as client:
        body = client.get(url, params={'page': 1, 'page_size': 10}).json()
    observation = Observation(request_id='req_001', url=url, query={'page': '1', 'page_size': '10'}, body=body)
    broken = ExtractionPlan(request_id='req_001', items_pointer='/missing',
                            fields={'id': '/id', 'name': '/name', 'price_fen': '/price_fen'},
                            unique_key='id', page_parameter='page',
                            has_next_pointer='/has_next', total_pointer='/total')

    class Gateway:
        calls = 0
        usage_history = []

        def __init__(self, configuration):
            pass

        async def generate(self, payload, schema):
            self.calls += 1
            return SimpleNamespace(data=broken)

    async def observe(*args):
        return [observation]

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    args = SimpleNamespace(url=shop_server + '/', click_text='', fields='id,name,price_fen',
                           max_pages=3, rag_enabled=False)
    assert asyncio.run(run.run_task(args, SimpleNamespace(model='fake'), output_root=tmp_path,
                                    quiet=True)) == 0
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))

    # 修复成功，且全程只有一次模型调用。
    assert report['status'] == 'succeeded'
    assert report['count'] == 30 and report['pages'] == 3 and report['completeness'] == 'complete'
    assert report['model_calls'] == 1
    assert report['repair_attempted'] is True and report['repair_count'] == 1
    assert report['repair_applied'] is True and report['repair_outcome'] == 'applied'

    # 谱系：original → candidate → execution。
    assert report['repair_original_plan_hash'] == plan_hash(broken)
    assert report['repair_candidate_plan_hash'] not in (None, report['repair_original_plan_hash'])
    assert report['repair_execution_plan_hash'] == report['repair_candidate_plan_hash']

    # 原计划从未被修改，也从未被覆盖。
    assert broken.items_pointer == '/missing'
    assert json.loads((folder / 'plan.json').read_text(encoding='utf-8')) == broken.model_dump()

    # collector 用真正执行过的计划导出，且与内部（修复后）结果逐项一致。
    assert report['collector_exported'] is True
    assert report['collector_execution_success'] is True
    assert report['collector_matches_internal_result'] is True
    collector = (folder / 'collector.py').read_text(encoding='utf-8')
    assert '"/items"' in collector and '"/missing"' not in collector
    verification = json.loads((folder / 'collector_verification.json').read_text(encoding='utf-8'))
    assert verification['matches'] is True
    assert all(verification['checks'].get(name) for name in
               ('records', 'record_count', 'pages', 'completeness', 'field_names', 'field_types', 'unique_keys'))

    # repair.json 扩展字段齐备。
    document = json.loads((folder / 'repair.json').read_text(encoding='utf-8'))
    assert document['applied'] is True
    assert document['original_plan_hash'] == plan_hash(broken)
    assert document['candidate_plan_hash'] == report['repair_candidate_plan_hash']
    assert document['execution_result'] == {'status': 'succeeded', 'count': 30, 'pages': 3,
                                            'completeness': 'complete'}
    assert document['attempts'][0]['reason_code'] == 'repairable'
    assert document['attempts'][0]['candidate_plan_hash'] == document['candidate_plan_hash']
    assert document['proposals'][0]['modified_fields'] == ['items_pointer']
