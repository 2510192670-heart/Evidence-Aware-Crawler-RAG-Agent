"""Pipeline error taxonomy: registry policy, fail-closed defaults and migration.

The taxonomy must never guess: a code decides the classification, an
unclassified failure stays unclassified, and structural details stay inside an
allowlist so nothing from a response body can reach a report.
"""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.app.llm.gateway import GatewayError
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.pipeline.errors import (
    MAX_DETAIL_VALUE_LENGTH,
    SPECS,
    Category,
    PipelineError,
    classify,
)
from backend.app.pipeline.execution import execute_plan
from backend.app.pipeline.export import build_collector_source
from benchmarks import load as load_manifest
from fixtures.app import app

FIXTURE_BASE = 'http://127.0.0.1:8100'
GATEWAY_PATH = Path(__file__).resolve().parents[1] / 'backend' / 'app' / 'llm' / 'gateway.py'

# 第一批迁移范围：改异常类型，错误码字符串不变。
MIGRATED_CODES = {
    'pointer_not_found': Category.PLAN_SEMANTIC,
    'duplicate_id': Category.DATA_INTEGRITY,
    'total_changed': Category.DATA_INTEGRITY,
    'field_type_changed': Category.DATA_INTEGRITY,
    'empty_page_with_next': Category.DATA_INTEGRITY,
}

GATEWAY_CODES = {
    'invalid_input', 'input_too_large', 'call_budget_exceeded', 'response_too_large',
    'authentication_failed', 'access_denied', 'rate_limited', 'provider_error',
    'request_rejected', 'redirect_rejected', 'connect_timeout', 'network_error',
    'read_timeout', 'write_timeout', 'pool_timeout', 'timeout',
    'invalid_output', 'output_incomplete',
}

DRIFT_TASKS = [task for task in load_manifest()['tasks'] if task['scenario'] == 'S5']


def fixture_client():
    """Serve the M4.1 fixture app in-process: no port, no proxy, no flakiness."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=FIXTURE_BASE)


def plan_for(task, **overrides):
    solution = task['solution']
    values = dict(request_id='req_001', items_pointer=solution['items_pointer'],
                  fields={name: '/' + name for name in task['fields']},
                  unique_key=solution['unique_key'],
                  page_parameter=solution['pagination_parameter'],
                  has_next_pointer=solution['has_next_pointer'],
                  total_pointer=solution['total_pointer'])
    values.update(overrides)
    return ExtractionPlan(**values)


async def observation_for(client, task):
    solution = task['solution']
    params = {solution['pagination_parameter']: 1, solution['page_size_parameter']: task['page_size']}
    response = await client.get(solution['request_path'], params=params)
    response.raise_for_status()
    return Observation(request_id='req_001', url=FIXTURE_BASE + solution['request_path'],
                       query={solution['pagination_parameter']: '1',
                              solution['page_size_parameter']: str(task['page_size'])},
                       body=response.json())


async def collect(client, task, plan):
    return await execute_plan(client, plan, [await observation_for(client, task)], task['max_pages'])


# --- PipelineError contract ---------------------------------------------

def test_pipeline_error_is_a_value_error_whose_message_is_the_code():
    error = PipelineError('duplicate_id')
    assert isinstance(error, ValueError)
    assert str(error) == 'duplicate_id'
    assert error.args == ('duplicate_id',)


@pytest.mark.parametrize('code,category', sorted(MIGRATED_CODES.items()))
def test_migrated_codes_keep_the_old_contract_and_gain_classification(code, category):
    error = PipelineError(code)
    assert isinstance(error, ValueError)      # 既有 pytest.raises(ValueError) 不破
    assert isinstance(error, PipelineError)
    assert str(error) == code
    assert error.category is category
    assert error.repairable is (category is Category.PLAN_SEMANTIC)
    assert error.retryable is False


def test_registry_drives_policy_fields():
    for code, spec in SPECS.items():
        error = PipelineError(code)
        assert error.category is spec.category, code
        assert error.repairable is spec.repairable, code
        assert error.retryable is spec.retryable, code
        assert error.detail_keys == spec.detail_keys, code


def test_callers_cannot_override_policy_fields():
    with pytest.raises(TypeError):
        PipelineError('duplicate_id', category=Category.SECURITY)
    with pytest.raises(TypeError):
        PipelineError('duplicate_id', repairable=True)
    with pytest.raises(TypeError):
        PipelineError('duplicate_id', retryable=True)


def test_unknown_code_fails_fast():
    with pytest.raises(KeyError):
        PipelineError('not_a_registered_code')


def test_registry_entries_are_well_formed():
    assert SPECS
    for code, spec in SPECS.items():
        assert isinstance(code, str) and code
        assert isinstance(spec.category, Category)
        assert spec.category is not Category.UNCLASSIFIED, code
        assert isinstance(spec.repairable, bool) and isinstance(spec.retryable, bool)
        assert isinstance(spec.detail_keys, frozenset)
        assert all(isinstance(key, str) and key for key in spec.detail_keys), code
        assert spec.summary.strip(), code


def test_capability_matrix_is_fail_closed():
    for code, spec in SPECS.items():
        if spec.category in {Category.SECURITY, Category.OBSERVATION,
                             Category.DATA_INTEGRITY, Category.BUDGET}:
            assert not spec.repairable, code
            assert not spec.retryable, code
        elif spec.category in {Category.PLAN_SEMANTIC, Category.MODEL_OUTPUT}:
            assert spec.repairable, code
            assert not spec.retryable, code
        else:
            assert spec.category is Category.TRANSPORT, code
            assert not spec.repairable, code
    assert {code for code, spec in SPECS.items() if spec.retryable} == {'connect_timeout', 'network_error'}


# --- classification fail-closed -----------------------------------------

def test_classify_never_parses_messages():
    for error in (ValueError('duplicate_id'), RuntimeError('pointer_not_found'), KeyError('x')):
        classification = classify(error)
        assert classification.category is Category.UNCLASSIFIED
        assert classification.repairable is False
        assert classification.retryable is False
    assert classify(ValueError('duplicate_id')).code == 'ValueError'


def test_gateway_codes_map_through_the_registry():
    invalid_output = classify(GatewayError('invalid_output'))
    assert invalid_output.category is Category.MODEL_OUTPUT
    assert invalid_output.repairable is True
    assert classify(GatewayError('authentication_failed')).category is Category.BUDGET
    assert classify(GatewayError('connect_timeout')).retryable is True


def test_every_declared_gateway_code_is_registered():
    discovered = set()
    for line in GATEWAY_PATH.read_text(encoding='utf-8').splitlines():
        if 'GatewayError' in line or line.strip().startswith('codes = {'):
            discovered.update(re.findall(r"'([a-z][a-z_]*)'", line))
    assert discovered, 'gateway error codes not found'
    assert discovered <= set(SPECS)
    assert GATEWAY_CODES <= set(SPECS)


# --- details allowlist --------------------------------------------------

def test_details_allowlist_and_scalar_contract():
    error = PipelineError('duplicate_id', details={'page': 3, 'unique_key': 'id'})
    assert error.details == {'page': 3, 'unique_key': 'id'}
    assert json.dumps(error.details)

    with pytest.raises(ValueError):
        PipelineError('duplicate_id', details={'record': 'leaked'})
    with pytest.raises(ValueError):
        PipelineError('duplicate_id', details=['page'])
    with pytest.raises(ValueError):
        PipelineError('duplicate_id', details={'page': ['1']})
    with pytest.raises(ValueError):
        PipelineError('duplicate_id', details={'page': True})


def test_overlong_detail_values_are_truncated_not_raised():
    error = PipelineError('pointer_not_found', details={'pointer': '/' + 'a' * 500})
    assert len(error.details['pointer']) == MAX_DETAIL_VALUE_LENGTH
    assert str(error) == 'pointer_not_found'


# --- real raise sites ---------------------------------------------------

@pytest.mark.parametrize('task', DRIFT_TASKS, ids=[t['id'] for t in DRIFT_TASKS])
def test_s5_drift_fixtures_are_classified_as_data_integrity(task):
    async def scenario():
        async with fixture_client() as client:
            with pytest.raises(PipelineError) as caught:
                await collect(client, task, plan_for(task))
            return caught.value

    error = asyncio.run(scenario())
    assert error.code == task['expected']['failure_kind']
    assert str(error) == task['expected']['failure_kind']
    assert error.category is Category.DATA_INTEGRITY
    assert error.repairable is False
    assert error.retryable is False
    assert set(error.details) <= error.detail_keys


def test_pointer_not_found_is_plan_semantic_and_repairable():
    task = next(task for task in load_manifest()['tasks'] if task['id'] == 's1-dev-1')

    async def scenario():
        async with fixture_client() as client:
            record = await observation_for(client, task)
            with pytest.raises(PipelineError) as caught:
                await execute_plan(client, plan_for(task, items_pointer='/missing'), [record], task['max_pages'])
            return caught.value

    error = asyncio.run(scenario())
    assert error.code == 'pointer_not_found'
    assert str(error) == 'pointer_not_found'
    assert error.category is Category.PLAN_SEMANTIC
    assert error.repairable is True
    assert error.details == {'pointer': '/missing'}


def test_details_never_carry_sample_values():
    task = DRIFT_TASKS[0]

    async def scenario():
        async with fixture_client() as client:
            with pytest.raises(PipelineError) as caught:
                await collect(client, task, plan_for(task))
            return caught.value

    error = asyncio.run(scenario())
    assert error.details == {'page': 2, 'unique_key': task['solution']['unique_key']}
    text = json.dumps(error.details, ensure_ascii=False)
    for marker in ('样品', '台账', '物料', '条目'):
        assert marker not in text


# --- run.py report ------------------------------------------------------

def run_failing_task(tmp_path, monkeypatch, error, fields='id'):
    class Gateway:
        calls = 0
        usage_history = []

        def __init__(self, config):
            pass

        async def generate(self, payload, schema):
            self.calls += 1
            return SimpleNamespace(data=ExtractionPlan(
                request_id='r', items_pointer='/items', fields={'id': '/id'}, unique_key='id',
                page_parameter='page', has_next_pointer=None, total_pointer=None))

    async def observe(*args):
        return [Observation(request_id='r', url=FIXTURE_BASE + '/catalog/v1/items',
                            query={'page': '1'}, body={'items': [{'id': 1}]})]

    async def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(run, 'execute_plan', broken)
    args = SimpleNamespace(url=FIXTURE_BASE + '/', click_text='', fields=fields,
                           max_pages=1, rag_enabled=False)
    asyncio.run(run.run_task(args, SimpleNamespace(model='fake'), output_root=tmp_path, quiet=True))
    folder = next((tmp_path / 'tasks').iterdir())
    return json.loads((folder / 'report.json').read_text(encoding='utf-8'))


def test_report_records_classification_for_pipeline_errors(tmp_path, monkeypatch):
    report = run_failing_task(tmp_path, monkeypatch, PipelineError(
        'duplicate_id', details={'page': 2, 'unique_key': 'id'}))
    assert report['status'] == 'failed'
    assert report['error'] == 'duplicate_id'
    assert report['error_type'] == 'PipelineError'
    assert report['error_category'] == 'DATA_INTEGRITY'
    assert report['error_repairable'] is False
    assert report['error_retryable'] is False


@pytest.mark.parametrize('error,expected_error,expected_category,expected_repairable', [
    (GatewayError('authentication_failed'), 'authentication_failed', 'BUDGET', False),
    (GatewayError('invalid_output'), 'invalid_output', 'MODEL_OUTPUT', True),
    (RuntimeError('boom'), 'RuntimeError', 'UNCLASSIFIED', False),
    (ValueError('duplicate_id'), 'ValueError', 'UNCLASSIFIED', False),
])
def test_report_classifies_gateway_and_unmigrated_failures(tmp_path, monkeypatch, error,
                                                           expected_error, expected_category,
                                                           expected_repairable):
    report = run_failing_task(tmp_path, monkeypatch, error)
    assert report['error'] == expected_error
    assert report['error_category'] == expected_category
    assert report['error_repairable'] is expected_repairable


def test_report_keeps_existing_fields_and_does_not_leak_values(tmp_path, monkeypatch):
    report = run_failing_task(tmp_path, monkeypatch, PipelineError('total_changed', details={'page': 2}))
    assert {'task_id', 'status', 'model', 'source', 'retrieval',
            'elapsed_seconds', 'model_calls', 'usage', 'error'} <= set(report)
    text = json.dumps(report, ensure_ascii=False)
    for marker in ('样品', '台账', '物料', '条目'):
        assert marker not in text


# --- collector template isolation ---------------------------------------

def export_plan():
    return ExtractionPlan(request_id='req_001', items_pointer='/items',
                          fields={'id': '/id', 'name': '/name'}, unique_key='id',
                          page_parameter='page', has_next_pointer='/has_next', total_pointer='/total')


def export_record():
    return Observation(request_id='req_001', url='http://127.0.0.1:8000/api/products',
                       query={'page': '1', 'page_size': '10'},
                       body={'total': 1, 'has_next': False, 'items': [{'id': 1, 'name': 'x'}]})


def test_collector_template_stays_independent_of_the_taxonomy():
    source = build_collector_source(export_plan(), export_record(), 3)
    assert 'PipelineError' not in source
    assert 'backend' not in source
    assert "RuntimeError('duplicate_id')" in source


def test_export_validation_is_not_taxonomy_coupled():
    bad_plan = export_plan().model_copy(update={'page_parameter': 'invented'})
    with pytest.raises(ValueError) as caught:
        build_collector_source(bad_plan, export_record(), 3)
    assert not isinstance(caught.value, PipelineError)
