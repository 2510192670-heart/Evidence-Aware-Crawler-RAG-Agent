"""M5.3-C: runtime failure-retrieval evaluation and freeze evidence.

Four-group ablation over one fixture corpus. A / B / C are the retrieval-layer
groups frozen by M5.2-C (A = BM25 only, B = BM25 + feature gate, C = BM25 +
feature gate + failure-aware knowledge). D is the new axis: the runtime
projection path ``run.py`` uses on a failure branch --

    errors.classify()  ->  failure.build_failure_context()
                       ->  retrieval.retrieve(..., failure_context=)
                       ->  failure.build_failure_retrieval()
                       ->  failure_retrieval.json

D's ``failure_context`` is derived from :func:`errors.classify` only, so the
category and phase can never be forged by the caller. D is advisory only: it
never re-plans, re-observes, executes, repairs or calls a model.

The fixture is built in this file and is never written to ``cases.json`` or
``benchmarks/``. This file measures determinism, explainability and safety
consistency on a small controlled fixture; it is not a benchmark and it does not
claim a system success-rate improvement.
"""

import ast
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import HASH_PATH, content_sha256, load
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.pipeline.errors import SPECS, Category, PipelineError, classify
from backend.app.pipeline.repair import APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS, PRE_COLLECTION_PHASES
from backend.app.rag import failure, knowledge, retrieval
from backend.app.rag.retrieval import retrieve

FROZEN_BENCHMARK_SHA256 = '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'
FROZEN_CASES_SHA256 = '678f87ece8bdb0aa4171f5f80b6b289cd773215639b7e30d82dc4953f5cba1f2'

LEGACY_KEYS = {'enabled', 'algorithm', 'corpus_version', 'corpus_sha256', 'cases'}
FEATURE_KEYS = {'feature_schema_version', 'query_features', 'gate'}
KNOWLEDGE_KEYS = {'case_schema_version', 'knowledge_gate'}
M52_RESULT_KEYS = LEGACY_KEYS | FEATURE_KEYS
FAILURE_ARTIFACT_KEYS = {'schema_version', 'failure_context', 'failure_phase', 'knowledge_gate',
                         'case_ids', 'corpus_sha256'}
REPAIR_KEYS = {'schema_version', 'attempts', 'proposals', 'original_plan_hash',
               'candidate_plan_hash', 'applied', 'execution_result'}

SUCCESS_FILES = {'evidence.json', 'retrieval.json', 'cloud_payload.json', 'plan.json',
                 'result.json', 'report.json', 'report.md'}
FAILED_FILES_M52 = {'evidence.json', 'retrieval.json', 'cloud_payload.json', 'plan.json',
                    'repair.json', 'report.json', 'report.md'}
FAILED_FILES_M53 = FAILED_FILES_M52 | {'failure_retrieval.json'}

EMPTY_GATE = {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}
AUTO_APPLIED = knowledge.AUTO_APPLIED
TOP_K = 2
RESTRICTED = frozenset({Category.SECURITY.value, Category.DATA_INTEGRITY.value,
                        Category.TRANSPORT.value, Category.UNCLASSIFIED.value})
# 受限类别中只有前三类有已注册错误码；UNCLASSIFIED 无注册码，由 fail-closed 路径覆盖。
RESTRICTED_CODES = {'invalid_url': 'SECURITY', 'duplicate_id': 'DATA_INTEGRITY',
                    'network_error': 'TRANSPORT'}

GET_SUMMARY = [{'request_id': 'r', 'method': 'GET', 'status': 200, 'query': {'page': '1'},
                'response_shape': {'items': [{'id': '<integer>'}],
                                   'has_next': '<boolean>', 'total': '<integer>'}}]


def full_corpus():
    """Fixture with a legacy case plus features / failure_modes / repairability."""
    return [
        {'id': 'get_pager', 'title': 'GET 翻页与指针失败知识', 'terms': ['items', 'gm'], 'guidance': 'g',
         'features': {'method': 'GET', 'page_location': 'query'},
         'failure_modes': [{'code': 'pointer_not_found', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'pointer_absent'}],
         'repairability': {'pointer_not_found': 'auto_applied'}},
        {'id': 'legacy_case', 'title': 'legacy 案例', 'terms': ['has_next', 'lm'], 'guidance': 'g'},
        {'id': 'loc_guard', 'title': '分页位置失败知识', 'terms': ['page', 'lg'], 'guidance': 'g',
         'features': {'method': 'GET', 'page_location': 'query'},
         'failure_modes': [{'code': 'page_location_mismatch', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'method_mismatch'}],
         'repairability': {'page_location_mismatch': 'proposal_only'}},
        {'id': 'nested_total', 'title': '总数指针失败知识', 'terms': ['id', 'nt'], 'guidance': 'g',
         'features': {'method': 'GET', 'page_location': 'query'},
         'failure_modes': [{'code': 'invalid_total_pointer', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'total_not_int'}]},
        {'id': 'post_pager', 'title': 'POST 翻页知识', 'terms': ['total', 'pp'], 'guidance': 'g',
         'features': {'method': 'POST', 'page_location': 'json_body'},
         'failure_modes': [{'code': 'pointer_not_found', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'pointer_absent'}],
         'repairability': {'pointer_not_found': 'auto_applied'}},
    ]


QUERIES = [
    {'expected': 'get_pager', 'error_code': 'pointer_not_found'},
    {'expected': 'loc_guard', 'error_code': 'page_location_mismatch'},
    {'expected': 'nested_total', 'error_code': 'invalid_total_pointer'},
    # 受限类别：只用于 unsafe_retrieval_rate（无匹配知识案例，不参与 top-k 指标）。
    {'expected': None, 'error_code': 'invalid_url'},
    {'expected': None, 'error_code': 'duplicate_id'},
    {'expected': None, 'error_code': 'network_error'},
]


def _strip(cases, keys):
    return [{key: value for key, value in case.items() if key not in keys} for case in cases]


def group_corpora():
    """A / B / C differ only by knowledge fields; D reuses C's corpus on the runtime path."""
    full = full_corpus()
    return {'bm25': _strip(full, ('features', 'failure_modes', 'repairability')),
            'feature_gate': _strip(full, ('failure_modes', 'repairability')),
            'failure_aware': full,
            'runtime_failure': full}


def _ranked(result):
    return [entry['id'] for entry in result['cases']]


def _observed_category(code):
    spec = SPECS.get(code)
    return spec.category.value if spec is not None else None


def runtime_artifact(error_code, cases):
    """Run the D-group runtime path for one error code and return its artifact."""
    error = PipelineError(error_code)
    context = failure.build_failure_context(error)
    if context is None:
        return None
    result = retrieve([GET_SUMMARY[0]], cases=cases, failure_context=context)
    return failure.build_failure_retrieval(error, result)


def _retrieval_ranked(cases, code):
    return _ranked(retrieve([GET_SUMMARY[0]], cases=cases,
                            failure_context={'error_code': code}))


def _runtime_ranked(cases, code):
    artifact = runtime_artifact(code, cases)
    return artifact['case_ids'] if artifact is not None else []


RANKERS = {'bm25': _retrieval_ranked, 'feature_gate': _retrieval_ranked,
           'failure_aware': _retrieval_ranked, 'runtime_failure': _runtime_ranked}


def _score_group(ranked_for, view):
    top1 = top2 = mrr = code_hits = labeled = 0.0
    unsafe = retrieved_restricted = 0
    for query in QUERIES:
        code, expected = query['error_code'], query['expected']
        ranked = ranked_for(code)
        if expected is not None:
            labeled += 1
            top1 += 1.0 if ranked[:1] == [expected] else 0.0
            top2 += 1.0 if expected in ranked else 0.0
            mrr += 1.0 / (ranked.index(expected) + 1) if expected in ranked else 0.0
            if any(code in {mode.code for mode in view[cid].failure_modes} for cid in ranked):
                code_hits += 1.0
        if _observed_category(code) in RESTRICTED:
            for cid in ranked:
                retrieved_restricted += 1
                if AUTO_APPLIED in view[cid].repairability.values():
                    unsafe += 1
    return {'top1_hit': round(top1 / labeled, 6), 'top2_hit': round(top2 / labeled, 6),
            'mrr': round(mrr / labeled, 6),
            f'failure_code_hit_at_{TOP_K}': round(code_hits / labeled, 6),
            'unsafe_retrieval_rate': round(unsafe / retrieved_restricted, 6)
            if retrieved_restricted else 0.0}


def evaluate():
    """Deterministic four-group ablation metrics over the fixture."""
    corpora = group_corpora()
    groups = {}
    for name, cases in corpora.items():
        view = {case.id: case for case in knowledge.load(cases)}
        ranker = RANKERS[name]
        groups[name] = _score_group(lambda code, c=cases, r=ranker: r(c, code), view)
    return {'groups': groups, 'repairability_consistency': repairability_consistency()}


def project_disposition(code):
    """Independent repairability projection from the taxonomy + repair policy constants."""
    spec = SPECS[code]
    if not spec.repairable or spec.phase not in PRE_COLLECTION_PHASES:
        return 'rejected'
    return AUTO_APPLIED if code in APPLICABLE_ERROR_CODES else 'proposal_only'


def repairability_consistency():
    """1.0 when every declared repairability equals the independent projection."""
    for case in knowledge.load(full_corpus()):
        for code, declared in case.repairability.items():
            if declared != project_disposition(code):
                return 0.0
    for code in SPECS:
        if knowledge.project_repairability(code) != project_disposition(code):
            return 0.0
    return 1.0


# --- evaluation design ---------------------------------------------------

def test_a_b_c_d_groups_share_query_and_corpus():
    groups = group_corpora()
    assert set(groups) == {'bm25', 'feature_gate', 'failure_aware', 'runtime_failure'}
    full = full_corpus()
    signature = {(case['id'], tuple(case['terms'])) for case in full}
    for cases in groups.values():
        assert {(case['id'], tuple(case['terms'])) for case in cases} == signature
    assert groups['bm25'] == _strip(full, ('features', 'failure_modes', 'repairability'))
    assert groups['feature_gate'] == _strip(full, ('failure_modes', 'repairability'))
    assert groups['failure_aware'] == full
    # D 复用 C 的语料，只在输入路径上不同（runtime classify 投影）。
    assert groups['runtime_failure'] == groups['failure_aware']
    # fixture 覆盖四类字段：legacy、features、failure_modes、repairability。
    assert any('features' in case for case in full)
    assert any('failure_modes' in case for case in full)
    assert any('repairability' in case for case in full)
    assert any(not {'features', 'failure_modes', 'repairability'} & set(case) for case in full)


def test_metrics_are_complete_and_deterministic():
    first = evaluate()
    assert first == evaluate()
    assert json.dumps(first, sort_keys=True) == json.dumps(evaluate(), sort_keys=True)
    assert set(first) == {'groups', 'repairability_consistency'}
    assert set(first['groups']) == {'bm25', 'feature_gate', 'failure_aware', 'runtime_failure'}
    metric_keys = {'top1_hit', 'top2_hit', 'mrr', f'failure_code_hit_at_{TOP_K}',
                   'unsafe_retrieval_rate'}
    for metrics in first['groups'].values():
        assert set(metrics) == metric_keys
        for value in metrics.values():
            assert type(value) is float and 0.0 <= value <= 1.0
    # D 组与 C 组同语料、同归一化上下文，因此排序指标完全一致：runtime 投影不引入新影响。
    assert first['groups']['runtime_failure'] == first['groups']['failure_aware']
    # 受控 fixture 上的确定性结果：只有携带失败知识的 C / D 组能命中失败码。
    assert first['groups']['bm25'] == {'top1_hit': round(1 / 3, 6), 'top2_hit': round(1 / 3, 6),
                                       'mrr': round(1 / 3, 6), f'failure_code_hit_at_{TOP_K}': 0.0,
                                       'unsafe_retrieval_rate': 0.0}
    assert first['groups']['feature_gate'] == {'top1_hit': round(1 / 3, 6), 'top2_hit': round(2 / 3, 6),
                                               'mrr': 0.5, f'failure_code_hit_at_{TOP_K}': 0.0,
                                               'unsafe_retrieval_rate': 0.0}
    assert first['groups']['failure_aware'] == {'top1_hit': 1.0, 'top2_hit': 1.0, 'mrr': 1.0,
                                                f'failure_code_hit_at_{TOP_K}': 1.0,
                                                'unsafe_retrieval_rate': 0.0}
    assert first['repairability_consistency'] == 1.0


# --- 1. failure_context source: errors.classify, not forgeable -----------

def test_failure_context_is_sourced_from_the_taxonomy():
    for code in ('pointer_not_found', 'page_location_mismatch', 'invalid_total_pointer',
                 'invalid_url', 'duplicate_id', 'network_error'):
        context = failure.build_failure_context(PipelineError(code))
        assert context == {'error_code': code, 'category': SPECS[code].category.value,
                           'phase': SPECS[code].phase}
    # 未注册的 code 不能凭属性自称分类：只能 fail-closed。
    forged = type('Forged', (ValueError,),
                  {'code': 'not_registered', 'category': 'SECURITY', 'phase': 'observing'})()
    assert failure.build_failure_context(forged) is None
    assert classify(forged).category is Category.UNCLASSIFIED


def test_runtime_group_cannot_forge_category_or_phase():
    forged = type('Forged', (ValueError,),
                  {'code': 'duplicate_id', 'category': 'SECURITY', 'phase': 'observing'})()
    assert failure.build_failure_context(forged) == {'error_code': 'duplicate_id',
                                                     'category': 'DATA_INTEGRITY',
                                                     'phase': 'executing'}
    result = retrieve([GET_SUMMARY[0]], cases=full_corpus(),
                      failure_context=failure.build_failure_context(forged))
    artifact = failure.build_failure_retrieval(forged, result)
    assert artifact['failure_context'] == {'error_code': 'duplicate_id',
                                           'category': 'DATA_INTEGRITY', 'phase': 'executing'}
    assert artifact['failure_phase'] == 'execution'


# --- 5. safety: unsafe_retrieval_rate == 0 -------------------------------

def test_unsafe_retrieval_rate_is_zero_for_every_group():
    metrics = evaluate()['groups']
    for name, values in metrics.items():
        assert values['unsafe_retrieval_rate'] == 0.0, name


@pytest.mark.parametrize('code,category', sorted(RESTRICTED_CODES.items()))
def test_restricted_categories_never_recommend_auto_applied(code, category):
    assert classify(PipelineError(code)).category.value == category
    artifact = runtime_artifact(code, full_corpus())
    auto_ids = {case['id'] for case in full_corpus()
                if AUTO_APPLIED in (case.get('repairability') or {}).values()}
    assert auto_ids, 'the fixture must contain an auto_applied case'
    assert artifact['knowledge_gate']['applied'] is True
    assert auto_ids.isdisjoint(artifact['case_ids'])
    # 边界过滤只会命中 auto_applied 案例；至少触发一次。
    filtered = artifact['knowledge_gate']['filtered']
    assert filtered and set(filtered) <= auto_ids


def test_unclassified_failure_is_refused():
    error = ValueError('boom')
    assert classify(error).category is Category.UNCLASSIFIED
    assert failure.build_failure_context(error) is None
    assert failure.build_failure_retrieval(error, {'cases': [], 'corpus_sha256': 'x'}) is None
    # 任意未注册异常同样 fail-closed，且不产出诊断产物。
    assert failure.build_failure_context(RuntimeError('boom')) is None
    # 对照：已注册失败仍产出诊断产物。
    assert runtime_artifact('duplicate_id', full_corpus()) is not None


# --- 4/8. advisory only, additive artifact -------------------------------

def test_runtime_diagnostic_is_advisory_only():
    artifact = runtime_artifact('pointer_not_found', full_corpus())
    assert set(artifact) == FAILURE_ARTIFACT_KEYS
    assert set(artifact['knowledge_gate']) == {'applied', 'filtered', 'matched', 'conflicted'}
    # 诊断产物只带闭集码、策展案例 id 与哈希，绝无 URL / 指针 / 凭据。
    dumped = json.dumps(artifact).lower()
    for forbidden in ('http', 'items_pointer', 'page_parameter', 'unique_key',
                      'authorization', 'cookie', 'request_id'):
        assert forbidden not in dumped, forbidden
    # repair 边界未被扩张。
    assert APPLICABLE_ERROR_CODES == frozenset({'pointer_not_found'})
    assert MAX_REPAIR_ATTEMPTS == 1
    # build_failure_retrieval 不修改输入。
    result = retrieve([GET_SUMMARY[0]], cases=full_corpus(),
                      failure_context=failure.build_failure_context(PipelineError('pointer_not_found')))
    before = copy.deepcopy(result)
    failure.build_failure_retrieval(PipelineError('pointer_not_found'), result)
    assert result == before


# --- 2/3/4. end-to-end pipeline ------------------------------------------

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


def test_pipeline_failure_keeps_one_model_call(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch, error=PipelineError('duplicate_id'))
    assert (folder / 'failure_retrieval.json').is_file()   # D 组确实运行
    assert report['model_calls'] == 1


def test_success_task_artifacts_are_unchanged_from_m52(tmp_path, monkeypatch):
    report, folder = run_pipeline(tmp_path, monkeypatch)
    assert report['status'] == 'succeeded'
    # 无失败任务产物集与 M5.2 完全一致，不追加诊断产物。
    assert files_of(folder) == SUCCESS_FILES
    assert 'failure_retrieval' not in report
    artifact = json.loads((folder / 'retrieval.json').read_text(encoding='utf-8'))
    assert set(artifact) == M52_RESULT_KEYS
    assert KNOWLEDGE_KEYS.isdisjoint(artifact)


def test_failure_task_artifact_is_additive(tmp_path, monkeypatch):
    error = PipelineError('duplicate_id')
    _, folder_on = run_pipeline(tmp_path / 'on', monkeypatch, error=error)
    _, folder_off = run_pipeline(tmp_path / 'off', monkeypatch, error=error, rag_enabled=False)
    # 失败任务只比 M5.2 多一个诊断产物，其余文件集不变。
    assert files_of(folder_on) == FAILED_FILES_M53
    assert files_of(folder_off) == FAILED_FILES_M52
    # retrieval.json 仍是 M5.2 契约，诊断不回写它。
    assert set(json.loads((folder_on / 'retrieval.json').read_text(encoding='utf-8'))) == M52_RESULT_KEYS
    # repair.json 不受 RAG/诊断影响，且不含任何检索字段。
    repair_on = json.loads((folder_on / 'repair.json').read_text(encoding='utf-8'))
    repair_off = json.loads((folder_off / 'repair.json').read_text(encoding='utf-8'))
    assert repair_on == repair_off
    assert set(repair_on) == REPAIR_KEYS
    assert 'failure_context' not in json.dumps(repair_on)


# --- 6. determinism ------------------------------------------------------

def test_output_is_deterministic(tmp_path, monkeypatch):
    error = PipelineError('duplicate_id')
    _, folder_a = run_pipeline(tmp_path / 'a', monkeypatch, error=error)
    _, folder_b = run_pipeline(tmp_path / 'b', monkeypatch, error=error)
    assert ((folder_a / 'failure_retrieval.json').read_text(encoding='utf-8')
            == (folder_b / 'failure_retrieval.json').read_text(encoding='utf-8'))
    assert (runtime_artifact('pointer_not_found', full_corpus())
            == runtime_artifact('pointer_not_found', full_corpus()))
    assert evaluate() == evaluate()


# --- compatibility freeze ------------------------------------------------

def test_frozen_corpus_and_benchmark_are_unaffected():
    assert content_sha256(load()) == FROZEN_BENCHMARK_SHA256
    assert HASH_PATH.read_text(encoding='utf-8').strip() == FROZEN_BENCHMARK_SHA256
    raw = Path(knowledge.__file__).with_name('cases.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    # 冻结 v1 语料对 failure_context 完全中性。
    assert retrieve(GET_SUMMARY) == retrieve(GET_SUMMARY,
                                             failure_context={'error_code': 'pointer_not_found'})
    assert set(retrieve(GET_SUMMARY)) == M52_RESULT_KEYS
    assert EMPTY_GATE == retrieve(GET_SUMMARY)['gate']


def test_runtime_modules_have_no_execution_or_provider_imports():
    source = Path(failure.__file__).read_text(encoding='utf-8')
    for forbidden in ('execute_plan', 'subprocess', 'RepairProposer', 'run_task',
                      'gateway', 'openai', 'httpx', 'requests'):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append('.' * node.level + (node.module or ''))
    assert modules == ['..pipeline.errors']
    for module in (retrieval, knowledge):
        text = Path(module.__file__).read_text(encoding='utf-8')
        for forbidden in ('execute_plan', 'subprocess', 'RepairProposer', 'run_task',
                          'gateway', 'openai', 'httpx', 'requests'):
            assert forbidden not in text, f'{module.__name__}:{forbidden}'
