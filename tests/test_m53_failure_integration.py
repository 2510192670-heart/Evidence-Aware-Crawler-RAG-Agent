"""M5.3-B: runtime failure-retrieval hardening (integration).

Verifies, on top of M5.3-A and without changing any architecture:

* the failure_phase mapping for every lifecycle bucket,
* that a v2 knowledge corpus activates the gate and yields matched cases,
* that restricted failures never recommend an ``auto_applied`` case,
* that the diagnostic artifact is strictly additive (repair.json / retrieval.json
  untouched),
* that a task without failure keeps the M5.2 artifact set,
* a single model call, and determinism.

The diagnostic is read-only: it never re-plans, re-observes, executes or repairs.
"""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.pipeline.errors import PipelineError, classify
from backend.app.rag import failure, knowledge
from backend.app.rag.retrieval import retrieve

GET_SUMMARY = [{'method': 'GET', 'query': {'page': '1'},
                'response_shape': {'items': [{'id': '<integer>'}],
                                   'has_next': '<boolean>', 'total': '<integer>'}}]
POINTER_NOT_FOUND = PipelineError('pointer_not_found', details={'pointer': '/items'})
DUPLICATE_ID = PipelineError('duplicate_id', details={'page': 2, 'unique_key': 'id'})

SUCCESS_FILES = {'evidence.json', 'retrieval.json', 'cloud_payload.json', 'plan.json',
                 'result.json', 'report.json', 'report.md'}
FAILED_FILES = {'evidence.json', 'retrieval.json', 'cloud_payload.json', 'plan.json',
                'repair.json', 'report.json', 'report.md', 'failure_retrieval.json'}
M52_RESULT_KEYS = {'enabled', 'algorithm', 'corpus_version', 'corpus_sha256', 'cases',
                   'feature_schema_version', 'query_features', 'gate'}
REPAIR_KEYS = {'schema_version', 'attempts', 'proposals', 'original_plan_hash',
               'candidate_plan_hash', 'applied', 'execution_result'}


def v2_corpus():
    """Knowledge corpus with features / failure_modes / repairability / legacy case."""
    return [
        {'id': 'zcase', 'title': 'auto 可修复失败知识', 'terms': ['items', 'zm'], 'guidance': 'g',
         'features': {'method': 'GET', 'page_location': 'query'},
         'failure_modes': [{'code': 'pointer_not_found', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'pointer_absent'}],
         'repairability': {'pointer_not_found': 'auto_applied'}},
        {'id': 'acase', 'title': '总数指针失败知识', 'terms': ['total', 'am'], 'guidance': 'g',
         'features': {'method': 'GET', 'page_location': 'query'},
         'failure_modes': [{'code': 'invalid_total_pointer', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'total_not_int'}]},
        {'id': 'plain', 'title': 'legacy 案例', 'terms': ['has_next', 'pm'], 'guidance': 'g'},
    ]


def auto_applied_ids(corpus):
    return {case['id'] for case in corpus
            if knowledge.AUTO_APPLIED in (case.get('repairability') or {}).values()}


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


def files_of(folder):
    return {path.name for path in folder.iterdir()}


# --- 1. failure_phase mapping -------------------------------------------

def test_failure_phase_maps_every_lifecycle_bucket():
    mapping = {
        PipelineError('no_usable_json_requests'): ('observation', 'observing'),
        POINTER_NOT_FOUND: ('planning', 'analyzing'),
        DUPLICATE_ID: ('execution', 'executing'),
        ValueError('boom'): ('unknown', 'unknown'),
    }
    for error, (expected, registry_phase) in mapping.items():
        classification = classify(error)
        assert classification.phase == registry_phase
        assert failure.failure_phase(classification) == expected
    assert set(failure.FAILURE_PHASES) == {'observation', 'planning', 'execution', 'unknown'}


# --- 2. a v2 knowledge corpus activates the gate -------------------------

def test_v2_knowledge_corpus_activates_the_gate():
    corpus = v2_corpus()
    context = failure.build_failure_context(POINTER_NOT_FOUND)
    result = retrieve(GET_SUMMARY, cases=corpus, failure_context=context)
    assert result['case_schema_version'] == 2
    assert 'knowledge_gate' in result
    artifact = failure.build_failure_retrieval(POINTER_NOT_FOUND, result)
    assert artifact['knowledge_gate']['applied'] is True
    assert artifact['knowledge_gate']['matched']            # 非空
    assert 'zcase' in artifact['knowledge_gate']['matched']
    assert artifact['case_ids'] == [case['id'] for case in result['cases']]
    assert artifact['corpus_sha256'] == result['corpus_sha256']


# --- 3. restricted failures never recommend auto_applied knowledge -------

@pytest.mark.parametrize('code', ['invalid_url', 'duplicate_id', 'network_error'])
def test_restricted_failures_never_recommend_auto_applied(code):
    error = PipelineError(code)
    assert classify(error).category.value in {'SECURITY', 'DATA_INTEGRITY', 'TRANSPORT'}
    corpus = v2_corpus()
    context = failure.build_failure_context(error)
    result = retrieve(GET_SUMMARY, cases=corpus, failure_context=context)
    artifact = failure.build_failure_retrieval(error, result)
    auto_ids = auto_applied_ids(corpus)
    assert auto_ids, 'the fixture must contain an auto_applied case'
    assert artifact['knowledge_gate']['applied'] is True
    assert auto_ids.isdisjoint(artifact['case_ids'])
    assert 'zcase' in artifact['knowledge_gate']['filtered']


# --- 4. the diagnostic artifact is strictly additive ---------------------

def test_artifact_isolation(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch, error=DUPLICATE_ID)
    assert report['status'] == 'failed'
    # 失败任务仅在 M5.2 产物集之外多出诊断产物，其余完全一致。
    assert files_of(folder) == FAILED_FILES
    # retrieval.json 仍是 M5.2 契约（诊断不回写它）。
    retrieval_artifact = json.loads((folder / 'retrieval.json').read_text(encoding='utf-8'))
    assert set(retrieval_artifact) == M52_RESULT_KEYS
    # repair.json 只含既有字段，未混入检索字段。
    repair_artifact = json.loads((folder / 'repair.json').read_text(encoding='utf-8'))
    assert set(repair_artifact) == REPAIR_KEYS
    assert 'failure_context' not in json.dumps(repair_artifact)


def test_diagnostic_does_not_mutate_its_inputs():
    corpus = v2_corpus()
    result = retrieve(GET_SUMMARY, cases=corpus, failure_context=failure.build_failure_context(POINTER_NOT_FOUND))
    before = copy.deepcopy(result)
    failure.build_failure_retrieval(POINTER_NOT_FOUND, result)
    assert result == before


# --- 5. a task without failure keeps the M5.2 artifact set --------------

def test_task_without_failure_matches_m52_artifacts(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch)
    assert report['status'] == 'succeeded'
    assert files_of(folder) == SUCCESS_FILES
    assert 'failure_retrieval' not in report
    assert set(json.loads((folder / 'retrieval.json').read_text(encoding='utf-8'))) == M52_RESULT_KEYS


# --- 6. a single model call ---------------------------------------------

def test_failure_diagnostic_keeps_one_model_call(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch, error=DUPLICATE_ID)
    assert (folder / 'failure_retrieval.json').is_file()
    assert report['model_calls'] == 1


# --- 7. deterministic output -------------------------------------------

def test_output_is_deterministic(tmp_path, monkeypatch):
    _, folder_a = run_pipeline(tmp_path / 'a', monkeypatch, error=DUPLICATE_ID)
    _, folder_b = run_pipeline(tmp_path / 'b', monkeypatch, error=DUPLICATE_ID)
    assert ((folder_a / 'failure_retrieval.json').read_text(encoding='utf-8')
            == (folder_b / 'failure_retrieval.json').read_text(encoding='utf-8'))
    error = POINTER_NOT_FOUND
    result = retrieve(GET_SUMMARY, cases=v2_corpus(), failure_context=failure.build_failure_context(error))
    assert (failure.build_failure_retrieval(error, result)
            == failure.build_failure_retrieval(error, result))
