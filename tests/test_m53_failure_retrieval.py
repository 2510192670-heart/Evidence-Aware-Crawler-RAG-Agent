"""M5.3-A: runtime failure_context feeds a diagnostic knowledge retrieval.

Covers the failure-path artifact, taxonomy-sourced context (no caller override),
fail-closed behaviour (UNCLASSIFIED, RAG disabled), unchanged success artifacts,
a single model call, an unchanged repair artifact and determinism. The diagnostic
is read-only: it never re-plans, re-observes, executes or repairs.
"""

import asyncio
import json
from types import SimpleNamespace

from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.pipeline.errors import PipelineError, classify
from backend.app.pipeline.repair import APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS
from backend.app.rag import failure
from backend.app.rag.retrieval import retrieve

DUP = PipelineError('duplicate_id', details={'page': 2, 'unique_key': 'id'})
GET_SUMMARY = [{'method': 'GET', 'query': {'page': '1'},
                'response_shape': {'items': [{'id': '<integer>'}],
                                   'has_next': '<boolean>', 'total': '<integer>'}}]
ARTIFACT_KEYS = {'schema_version', 'failure_context', 'failure_phase', 'knowledge_gate',
                 'case_ids', 'corpus_sha256'}
REPAIR_KEYS = {'schema_version', 'attempts', 'proposals', 'original_plan_hash',
               'candidate_plan_hash', 'applied', 'execution_result'}


def run_pipeline(tmp_path, monkeypatch, error=None, rag_enabled=True):
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
                           max_pages=1, rag_enabled=rag_enabled)
    asyncio.run(run.run_task(args, SimpleNamespace(model='fake-model'), output_root=tmp_path, quiet=True))
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    return report, folder


def artifact_of(folder):
    return json.loads((folder / 'failure_retrieval.json').read_text(encoding='utf-8'))


# --- 1. a failure produces failure_retrieval.json ------------------------

def test_failure_generates_the_diagnostic_artifact(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch, error=DUP)
    assert report['status'] == 'failed'
    assert (folder / 'failure_retrieval.json').is_file()
    artifact = artifact_of(folder)
    assert set(artifact) == ARTIFACT_KEYS
    assert artifact['schema_version'] == 1
    assert set(artifact['failure_context']) == {'error_code', 'category', 'phase'}
    assert set(artifact['knowledge_gate']) == {'applied', 'filtered', 'matched', 'conflicted'}
    assert all(isinstance(case_id, str) for case_id in artifact['case_ids'])
    # 只写闭集与哈希，不带 URL / 响应值。
    assert 'http' not in json.dumps(artifact)
    assert report['failure_retrieval'] == {'case_ids': artifact['case_ids'],
                                           'knowledge_gate_applied': artifact['knowledge_gate']['applied']}


# --- 2. the error code comes from errors.classify ------------------------

def test_error_code_is_projected_from_the_taxonomy(tmp_path, monkeypatch):
    _, folder = run_pipeline(tmp_path, monkeypatch, error=DUP)
    artifact = artifact_of(folder)
    classification = classify(DUP)
    assert artifact['failure_context'] == {'error_code': 'duplicate_id',
                                           'category': classification.category.value,
                                           'phase': classification.phase}
    assert artifact['failure_context']['category'] == 'DATA_INTEGRITY'
    assert artifact['failure_phase'] == 'execution'


# --- 3. a caller cannot override category or phase -----------------------

def test_caller_cannot_override_category_or_phase():
    forged = type('Forged', (ValueError,),
                  {'code': 'duplicate_id', 'category': 'SECURITY', 'phase': 'observing'})()
    assert failure.build_failure_context(forged) == {'error_code': 'duplicate_id',
                                                     'category': 'DATA_INTEGRITY', 'phase': 'executing'}
    artifact = failure.build_failure_retrieval(forged, {'cases': [], 'corpus_sha256': 'x'})
    assert artifact['failure_context']['category'] == 'DATA_INTEGRITY'
    assert artifact['failure_context']['phase'] == 'executing'
    assert artifact['failure_phase'] == 'execution'


# --- 4. UNCLASSIFIED is refused (fail closed) ----------------------------

def test_unclassified_failure_generates_no_artifact(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch, error=ValueError('boom'))
    assert report['status'] == 'failed'
    assert report['error'] == 'ValueError'
    assert not (folder / 'failure_retrieval.json').exists()
    assert 'failure_retrieval' not in report
    assert failure.build_failure_context(ValueError('boom')) is None


# --- 5. RAG disabled generates nothing -----------------------------------

def test_rag_disabled_generates_no_artifact(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch, error=DUP, rag_enabled=False)
    assert report['status'] == 'failed'
    assert not (folder / 'failure_retrieval.json').exists()
    assert 'failure_retrieval' not in report
    assert json.loads((folder / 'retrieval.json').read_text(encoding='utf-8'))['cases'] == []


# --- 6. a successful task keeps its artifacts unchanged ------------------

def test_successful_task_has_no_failure_artifact(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch)
    assert report['status'] == 'succeeded'
    assert 'failure_retrieval' not in report
    assert 'failure_retrieval.json' not in {path.name for path in folder.iterdir()}
    assert (folder / 'retrieval.json').is_file()


# --- 7. the diagnostic adds no model call --------------------------------

def test_diagnostic_adds_no_model_call(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch, error=DUP)
    assert (folder / 'failure_retrieval.json').is_file()   # re-retrieval really ran
    assert report['model_calls'] == 1


# --- 8. the repair artifact is unchanged --------------------------------

def test_repair_artifact_is_unaffected(tmp_path, monkeypatch):
    _, folder_on = run_pipeline(tmp_path / 'on', monkeypatch, error=DUP, rag_enabled=True)
    _, folder_off = run_pipeline(tmp_path / 'off', monkeypatch, error=DUP, rag_enabled=False)
    repair_on = json.loads((folder_on / 'repair.json').read_text(encoding='utf-8'))
    repair_off = json.loads((folder_off / 'repair.json').read_text(encoding='utf-8'))
    assert repair_on == repair_off
    assert set(repair_on) == REPAIR_KEYS
    assert repair_on['attempts'][0]['error_code'] == 'duplicate_id'
    assert repair_on['attempts'][0]['outcome'] == 'rejected'
    assert repair_on['applied'] is False


# --- 9. the repair boundary is unchanged --------------------------------

def test_repair_boundary_is_unchanged():
    assert APPLICABLE_ERROR_CODES == frozenset({'pointer_not_found'})
    assert MAX_REPAIR_ATTEMPTS == 1


# --- 10. deterministic output -------------------------------------------

def test_diagnostic_is_deterministic(tmp_path, monkeypatch):
    _, folder_a = run_pipeline(tmp_path / 'a', monkeypatch, error=DUP)
    _, folder_b = run_pipeline(tmp_path / 'b', monkeypatch, error=DUP)
    assert ((folder_a / 'failure_retrieval.json').read_text(encoding='utf-8')
            == (folder_b / 'failure_retrieval.json').read_text(encoding='utf-8'))
    result = retrieve(GET_SUMMARY)
    assert (failure.build_failure_retrieval(DUP, result)
            == failure.build_failure_retrieval(DUP, result))
