"""M4.4.2 deterministic repair proposals: generation, validation and audit.

A proposal is evidence, not an action. These tests pin that candidate generation
is deterministic and side-effect free, that it reuses the executor's own plan
validator, that forbidden categories can never propose, and that a task with a
proposal still executes exactly once and calls the model exactly once.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.app.llm.gateway import GatewayError
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation, plan_hash
from backend.app.pipeline.errors import PipelineError
from backend.app.pipeline.repair import (
    ProposalOutcome,
    ProposalReason,
    RepairContext,
    RepairProposal,
    RepairProposer,
    audit_document,
    audit_failure,
    propose,
)

HASH = 'a' * 64


def base_plan(**overrides):
    values = dict(request_id='req_001', items_pointer='/items', fields={'id': '/id'},
                  unique_key='id', page_parameter='page', has_next_pointer=None, total_pointer=None)
    values.update(overrides)
    return ExtractionPlan(**values)


def post_record(**overrides):
    values = dict(request_id='req_001', url='http://127.0.0.1:8100/catalog/v1/query', method='POST',
                  query={}, request_body={'page': 1}, body={'items': [{'id': 1}]})
    values.update(overrides)
    return Observation(**values)


def get_record(**overrides):
    values = dict(request_id='req_001', url='http://127.0.0.1:8100/catalog/v1/items',
                  query={'page': '1'}, body={'items': [{'id': 1}]})
    values.update(overrides)
    return Observation(**values)


# --- schema ---------------------------------------------------------------

def test_proposal_schema_serializes_and_rejects_programming_errors():
    proposal = RepairProposal(HASH, 'b' * 64, ('items_pointer',),
                              ProposalReason.POINTER_RELOCATED.value, 'valid',
                              ProposalOutcome.PROPOSED.value)
    assert proposal.to_dict() == {
        'original_plan_hash': HASH,
        'candidate_plan_hash': 'b' * 64,
        'modified_fields': ['items_pointer'],
        'reason_code': 'pointer_relocated',
        'validation_result': 'valid',
        'outcome': 'proposed',
    }
    assert json.loads(json.dumps(proposal.to_dict())) == proposal.to_dict()
    with pytest.raises(ValueError):
        RepairProposal(HASH, None, (), 'made_up', None, 'rejected')
    with pytest.raises(ValueError):
        RepairProposal(HASH, None, (), ProposalReason.NO_DETERMINISTIC_CANDIDATE.value, None, 'made_up')
    # 不变量：proposed 当且仅当有候选且校验通过。
    with pytest.raises(ValueError):
        RepairProposal(HASH, None, (), ProposalReason.POINTER_RELOCATED.value, 'valid', 'proposed')
    with pytest.raises(ValueError):
        RepairProposal(HASH, 'b' * 64, ('items_pointer',), ProposalReason.POINTER_RELOCATED.value,
                       'invalid:x', 'proposed')


# --- deterministic generation --------------------------------------------

def test_page_location_mismatch_proposes_the_observed_location():
    error = PipelineError('page_location_mismatch', details={'parameter': 'page'})
    to_post = RepairProposer().propose(error, base_plan(), post_record())
    assert (to_post.outcome, to_post.reason_code) == ('proposed', 'page_location_from_evidence')
    assert to_post.modified_fields == ('pagination_location',)
    assert to_post.validation_result == 'valid'
    to_get = RepairProposer().propose(error, base_plan(pagination_location='json_body'), get_record())
    assert (to_get.outcome, to_get.reason_code) == ('proposed', 'page_location_from_evidence')


def test_pointer_not_found_relocates_to_the_unique_observed_list():
    proposal = RepairProposer().propose(
        PipelineError('pointer_not_found', details={'pointer': '/missing'}),
        base_plan(items_pointer='/missing'), get_record(body={'items': [{'id': 1}], 'total': 1}))
    assert (proposal.outcome, proposal.reason_code) == ('proposed', 'pointer_relocated')
    assert proposal.modified_fields == ('items_pointer',)
    assert proposal.validation_result == 'valid'


def test_ambiguous_or_absent_pointer_relocation_fails_closed():
    error = PipelineError('pointer_not_found', details={'pointer': '/missing'})
    ambiguous = RepairProposer().propose(
        error, base_plan(items_pointer='/missing'),
        get_record(body={'items': [{'id': 1}], 'others': [{'id': 2}]}))
    assert (ambiguous.outcome, ambiguous.reason_code) == ('rejected', 'no_deterministic_candidate')
    assert ambiguous.candidate_plan_hash is None
    # 字段内指针不是一个计划属性，没有确定性迁移依据。
    field_pointer = RepairProposer().propose(
        PipelineError('pointer_not_found', details={'pointer': '/title'}), base_plan(), get_record())
    assert field_pointer.outcome == 'rejected'


def test_invalid_page_field_type_is_recognized_but_has_no_candidate():
    proposal = RepairProposer().propose(
        PipelineError('invalid_page_field_type', details={'parameter': 'page'}),
        base_plan(pagination_location='json_body'), post_record(request_body={'page': '1'}))
    assert (proposal.outcome, proposal.reason_code) == ('rejected', 'no_deterministic_candidate')
    assert proposal.validation_result is None


# --- 1. proposal deterministic -------------------------------------------

def test_proposal_is_deterministic():
    plan, record = base_plan(), post_record()
    error = PipelineError('page_location_mismatch', details={'parameter': 'page'})
    first = RepairProposer().propose(error, plan, record)
    second = RepairProposer().propose(error, plan, record)
    assert first.to_dict() == second.to_dict()
    assert first.candidate_plan_hash == second.candidate_plan_hash
    assert propose(error, plan, record).to_dict() == first.to_dict()


# --- 2. original plan unchanged ------------------------------------------

def test_proposal_never_mutates_the_original_plan():
    plan = base_plan()
    snapshot, digest = plan.model_dump(), plan_hash(plan)
    RepairProposer().propose(PipelineError('page_location_mismatch', details={'parameter': 'page'}),
                             plan, post_record())
    assert plan.model_dump() == snapshot
    assert plan_hash(plan) == digest
    assert plan.pagination_location == 'query'


# --- 3. hash changes ------------------------------------------------------

def test_proposal_hashes_pin_original_and_changed_candidate():
    plan = base_plan()
    proposal = RepairProposer().propose(
        PipelineError('page_location_mismatch', details={'parameter': 'page'}), plan, post_record())
    assert proposal.original_plan_hash == plan_hash(plan)
    assert proposal.candidate_plan_hash is not None
    assert proposal.candidate_plan_hash != proposal.original_plan_hash


# --- 4. invalid proposal rejected ----------------------------------------

def test_candidate_failing_pre_flight_validation_is_rejected():
    def broken(error, plan, record):
        return (plan.model_copy(update={'items_pointer': '/missing'}), ('items_pointer',),
                ProposalReason.POINTER_RELOCATED)

    proposal = RepairProposer({'pointer_not_found': broken}).propose(
        PipelineError('pointer_not_found', details={'pointer': '/items'}), base_plan(), get_record())
    assert proposal.outcome == 'rejected'
    assert proposal.validation_result == 'invalid:pointer_not_found'
    assert proposal.candidate_plan_hash is not None


# --- 5. forbidden category never proposes --------------------------------

@pytest.mark.parametrize('error', [
    PipelineError('sensitive_pointer', details={'pointer': '/token'}),
    PipelineError('duplicate_id', details={'page': 2, 'unique_key': 'id'}),
    GatewayError('connect_timeout'),
    RuntimeError('boom'),
], ids=['security', 'data_integrity', 'transport', 'unclassified'])
def test_forbidden_categories_never_propose(error):
    attempt, proposal = audit_failure(error, RepairContext(original_plan_hash=HASH), base_plan(), post_record())
    assert proposal is None
    assert attempt.outcome == 'rejected'


def test_audit_document_always_carries_proposals():
    attempt, proposal = audit_failure(
        PipelineError('page_location_mismatch', details={'parameter': 'page'}),
        RepairContext(original_plan_hash=HASH), base_plan(), post_record())
    document = audit_document([attempt], [proposal])
    assert document['schema_version'] == 1
    assert document['proposals'][0]['outcome'] == 'proposed'
    # 禁止类别：只有 attempt，没有 proposal。
    rejected, none_proposal = audit_failure(RuntimeError('boom'), RepairContext(original_plan_hash=HASH),
                                            base_plan(), post_record())
    assert audit_document([rejected], [])['proposals'] == []


# --- 6 / 7. no second execution, no second model call --------------------

def test_proposal_never_re_executes_or_calls_the_model_again(tmp_path, monkeypatch):
    calls = {'execute': 0}

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
        return [post_record()]

    async def fake_execute(*args, **kwargs):
        calls['execute'] += 1
        raise PipelineError('page_location_mismatch', details={'parameter': 'page'})

    async def fake_export(*args, **kwargs):
        pytest.fail('collector export must not run after a failed collection')

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(run, 'execute_plan', fake_execute)
    monkeypatch.setattr(run, 'export_collector', fake_export)
    args = SimpleNamespace(url='http://127.0.0.1:8100/', click_text='', fields='id',
                           max_pages=1, rag_enabled=False)
    asyncio.run(run.run_task(args, SimpleNamespace(model='fake-model'), output_root=tmp_path, quiet=True))
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert calls['execute'] == 1
    assert report['model_calls'] == 1
    assert report['status'] == 'failed'
    assert report['repair_attempted'] is False and report['repair_count'] == 0
    assert report['repair_outcome'] == 'deferred'
    document = json.loads((folder / 'repair.json').read_text(encoding='utf-8'))
    assert len(document['attempts']) == 1
    proposal = document['proposals'][0]
    assert proposal['outcome'] == 'proposed'
    assert proposal['reason_code'] == 'page_location_from_evidence'
    assert proposal['modified_fields'] == ['pagination_location']
    assert proposal['candidate_plan_hash'] != proposal['original_plan_hash']
    # 审计只保留哈希与字段名，不落任何指针或响应值。
    assert '/items' not in json.dumps(document, ensure_ascii=False)
