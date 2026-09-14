"""M5.2-C: deterministic three-group evaluation and freeze evidence.

Ablation over one fixture corpus (never written to ``cases.json`` or
``benchmarks/``): A = BM25 only, B = BM25 + feature gate, C = BM25 + feature gate
+ failure-aware knowledge retrieval. The three groups share the same query and
the same corpus; only the knowledge fields (``features`` / ``failure_modes`` /
``repairability``) are added.

This file measures determinism, explainability and safety consistency on a small
controlled fixture. It does not claim a general accuracy improvement.
"""

import ast
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import HASH_PATH, content_sha256, load
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.pipeline.errors import SPECS, Category
from backend.app.pipeline.repair import APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS, PRE_COLLECTION_PHASES
from backend.app.rag import knowledge, retrieval
from backend.app.rag.retrieval import retrieve

FROZEN_BENCHMARK_SHA256 = '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'
FROZEN_CASES_SHA256 = '678f87ece8bdb0aa4171f5f80b6b289cd773215639b7e30d82dc4953f5cba1f2'
LEGACY_KEYS = {'enabled', 'algorithm', 'corpus_version', 'corpus_sha256', 'cases'}
FEATURE_KEYS = {'feature_schema_version', 'query_features', 'gate'}
KNOWLEDGE_KEYS = {'case_schema_version', 'knowledge_gate'}
EMPTY_GATE = {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}
AUTO_APPLIED = getattr(knowledge, 'AUTO_APPLIED')
TOP_K = 2
RESTRICTED = frozenset({Category.SECURITY.value, Category.DATA_INTEGRITY.value,
                        Category.TRANSPORT.value, Category.UNCLASSIFIED.value})

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
    {'expected': 'get_pager', 'failure_context': {'error_code': 'pointer_not_found'}},
    {'expected': 'loc_guard', 'failure_context': {'error_code': 'page_location_mismatch'}},
    {'expected': 'nested_total', 'failure_context': {'error_code': 'invalid_total_pointer'}},
    # 受限类别：只用于 unsafe_retrieval_rate（无对应知识案例，不参与 top-k 指标）。
    {'expected': None, 'failure_context': {'error_code': 'invalid_url'}},
]


def _strip(cases, keys):
    return [{key: value for key, value in case.items() if key not in keys} for case in cases]


def group_corpora():
    """A / B / C differ only by knowledge fields, over the same corpus and query."""
    full = full_corpus()
    return {'bm25': _strip(full, ('features', 'failure_modes', 'repairability')),
            'feature_gate': _strip(full, ('failure_modes', 'repairability')),
            'failure_aware': full}


def _ranked(result):
    return [entry['id'] for entry in result['cases']]


def _observed_category(failure_context):
    spec = SPECS.get((failure_context or {}).get('error_code'))
    return spec.category.value if spec is not None else None


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
    # 交叉检查实现投影与独立投影对所有注册码都一致。
    for code in SPECS:
        if knowledge.project_repairability(code) != project_disposition(code):
            return 0.0
    return 1.0


def evaluate():
    """Deterministic three-group ablation metrics over the fixture."""
    groups = {}
    for name, corpus_cases in group_corpora().items():
        view = {case.id: case for case in knowledge.load(corpus_cases)}
        top1 = top2 = mrr = code_hits = labeled = 0.0
        unsafe = retrieved_restricted = 0
        for query in QUERIES:
            result = retrieve([GET_SUMMARY[0]], cases=corpus_cases,
                              failure_context=query['failure_context'])
            ranked = _ranked(result)
            expected = query['expected']
            if expected is not None:
                labeled += 1
                top1 += 1.0 if ranked[:1] == [expected] else 0.0
                top2 += 1.0 if expected in ranked else 0.0
                mrr += 1.0 / (ranked.index(expected) + 1) if expected in ranked else 0.0
                code = query['failure_context']['error_code']
                if any(code in {mode.code for mode in view[case_id].failure_modes} for case_id in ranked):
                    code_hits += 1.0
            if _observed_category(query['failure_context']) in RESTRICTED:
                for case_id in ranked:
                    retrieved_restricted += 1
                    if AUTO_APPLIED in view[case_id].repairability.values():
                        unsafe += 1
        groups[name] = {'top1_hit': round(top1 / labeled, 6), 'top2_hit': round(top2 / labeled, 6),
                        'mrr': round(mrr / labeled, 6),
                        f'failure_code_hit_at_{TOP_K}': round(code_hits / labeled, 6),
                        'unsafe_retrieval_rate': round(unsafe / retrieved_restricted, 6)
                        if retrieved_restricted else 0.0}
    return {'groups': groups, 'repairability_consistency': repairability_consistency()}


# --- evaluation design --------------------------------------------------

def test_three_groups_share_query_and_corpus_and_differ_only_by_knowledge():
    groups = group_corpora()
    full = full_corpus()
    signature = {(case['id'], tuple(case['terms'])) for case in full}
    for name, corpus_cases in groups.items():
        assert {(case['id'], tuple(case['terms'])) for case in corpus_cases} == signature
    assert groups['bm25'] == _strip(full, ('features', 'failure_modes', 'repairability'))
    assert groups['feature_gate'] == _strip(full, ('failure_modes', 'repairability'))
    assert groups['failure_aware'] == full
    # fixture 覆盖四类字段：legacy、features、failure_modes、repairability。
    assert any('features' in case for case in full)
    assert any('failure_modes' in case for case in full)
    assert any('repairability' in case for case in full)
    assert any(not {'features', 'failure_modes', 'repairability'} & set(case) for case in full)


def test_metrics_are_complete_and_deterministic():
    first = evaluate()
    assert first == evaluate()
    assert set(first) == {'groups', 'repairability_consistency'}
    assert set(first['groups']) == {'bm25', 'feature_gate', 'failure_aware'}
    metric_keys = {'top1_hit', 'top2_hit', 'mrr', f'failure_code_hit_at_{TOP_K}',
                   'unsafe_retrieval_rate'}
    for metrics in first['groups'].values():
        assert set(metrics) == metric_keys
        for value in metrics.values():
            assert type(value) is float and 0.0 <= value <= 1.0
    # 受控 fixture 上的确定性结果：仅 C 组携带失败知识。
    assert first['groups']['bm25']['top1_hit'] == round(1 / 3, 6)
    assert first['groups']['bm25'][f'failure_code_hit_at_{TOP_K}'] == 0.0
    assert first['groups']['feature_gate'][f'failure_code_hit_at_{TOP_K}'] == 0.0
    assert first['groups']['failure_aware'] == {'top1_hit': 1.0, 'top2_hit': 1.0, 'mrr': 1.0,
                                                f'failure_code_hit_at_{TOP_K}': 1.0,
                                                'unsafe_retrieval_rate': 0.0}


# --- repair consistency -------------------------------------------------

def test_repairability_consistency_is_one():
    assert evaluate()['repairability_consistency'] == 1.0
    # 声明值必须等于独立投影；故意不一致会被检出。
    assert project_disposition('pointer_not_found') == AUTO_APPLIED
    assert project_disposition('page_location_mismatch') == 'proposal_only'
    assert knowledge.project_repairability('pointer_not_found') == AUTO_APPLIED


# --- unsafe retrieval rate ----------------------------------------------

def test_unsafe_retrieval_rate_is_zero():
    groups = evaluate()['groups']
    for metrics in groups.values():
        assert metrics['unsafe_retrieval_rate'] == 0.0
    # C 组面对受限失败时会过滤掉 auto_applied 知识案例。
    gated = retrieve([GET_SUMMARY[0]], cases=full_corpus(),
                     failure_context={'error_code': 'invalid_url'})
    assert 'get_pager' in gated['knowledge_gate']['filtered']
    assert 'get_pager' not in _ranked(gated)


# --- legacy corpus ablation ---------------------------------------------

def test_legacy_corpus_groups_are_identical():
    legacy = [{'id': 'l1', 'title': 't', 'terms': ['items', 'x'], 'guidance': 'g'},
              {'id': 'l2', 'title': 't', 'terms': ['total', 'y'], 'guidance': 'g'},
              {'id': 'l3', 'title': 't', 'terms': ['has_next', 'z'], 'guidance': 'g'}]
    variants = [_strip(legacy, ('features', 'failure_modes', 'repairability')),
                _strip(legacy, ('failure_modes', 'repairability')),
                _strip(legacy, ())]
    results = [retrieve(GET_SUMMARY, cases=corpus_cases,
                        failure_context={'error_code': 'pointer_not_found'}) for corpus_cases in variants]
    assert results[0] == results[1] == results[2]
    for result in results:
        assert set(result) == LEGACY_KEYS | FEATURE_KEYS     # knowledge 字段缺失 -> 阶段停用
        assert result['gate'] == EMPTY_GATE
        assert all(entry['id'] in {case['id'] for case in legacy} for entry in result['cases'])


def test_frozen_corpus_is_unaffected_by_failure_context():
    assert retrieve(GET_SUMMARY) == retrieve(GET_SUMMARY,
                                             failure_context={'error_code': 'pointer_not_found'})
    assert set(retrieve(GET_SUMMARY)) == LEGACY_KEYS | FEATURE_KEYS


# --- compatibility freeze ------------------------------------------------

def test_frozen_cases_json_sha256_is_unchanged():
    raw = Path(knowledge.__file__).with_name('cases.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256


def test_benchmark_manifest_and_hash_are_unchanged():
    assert content_sha256(load()) == FROZEN_BENCHMARK_SHA256
    assert HASH_PATH.read_text(encoding='utf-8').strip() == FROZEN_BENCHMARK_SHA256


def test_disabled_rag_keeps_the_legacy_five_keys():
    disabled = retrieve(GET_SUMMARY, enabled=False)
    assert set(disabled) == LEGACY_KEYS
    assert disabled['cases'] == []
    assert disabled == retrieve([], enabled=False)
    assert disabled == retrieve(GET_SUMMARY, enabled=False, failure_context={'error_code': 'pointer_not_found'})


def test_same_input_is_deterministic():
    first = retrieve(GET_SUMMARY, cases=full_corpus(), failure_context={'error_code': 'pointer_not_found'})
    second = retrieve(GET_SUMMARY, cases=full_corpus(), failure_context={'error_code': 'pointer_not_found'})
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- safety boundary -----------------------------------------------------

def _imported_modules(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append('.' * node.level + (node.module or ''))
    return modules


def test_failure_aware_can_only_influence_retrieval():
    for module in (retrieval, knowledge):
        source = Path(module.__file__).read_text(encoding='utf-8')
        for forbidden in ('execute_plan', 'subprocess', 'RepairProposer', 'evaluate_failure',
                          'run_task', 'gateway', 'openai', 'httpx', 'requests'):
            assert forbidden not in source, f'{module.__name__}:{forbidden}'
    # knowledge 只读 repair 的策略常量，不引入 repair 执行逻辑。
    assert [m for m in _imported_modules(knowledge.__file__) if 'repair' in m] == ['..pipeline.repair']


def test_retrieval_module_has_no_provider_or_execution_imports():
    joined = ' '.join(_imported_modules(retrieval.__file__))
    for forbidden in ('gateway', 'openai', 'httpx', 'requests', 'execution', 'llm'):
        assert forbidden not in joined, forbidden


def test_repair_boundary_is_not_expanded():
    assert APPLICABLE_ERROR_CODES == frozenset({'pointer_not_found'})
    assert MAX_REPAIR_ATTEMPTS == 1


def test_pipeline_model_calls_stay_at_one(tmp_path, monkeypatch):
    captured = []

    class Gateway:
        calls = 0
        usage_history = []

        def __init__(self, config):
            pass

        async def generate(self, payload, schema):
            captured.append(payload)
            self.calls += 1
            return SimpleNamespace(data=ExtractionPlan(request_id='r', items_pointer='/items',
                                                       fields={'id': '/id'}, unique_key='id',
                                                       page_parameter='page',
                                                       has_next_pointer='/has_next',
                                                       total_pointer='/total'))

    async def observe(*args):
        return [Observation(request_id='r', url='http://127.0.0.1:8000/api/products',
                            query={'page': '1'},
                            body={'items': [{'id': 1}], 'has_next': False, 'total': 1})]

    async def execute(*args):
        return {'items': [{'id': 1}], 'pages': 1, 'completeness': 'complete', 'expected_total': 1}

    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(run, 'execute_plan', execute)
    args = SimpleNamespace(url='http://127.0.0.1:8000/', click_text='', fields='id', max_pages=1,
                           rag_enabled=True)
    assert asyncio.run(run.run_task(args, SimpleNamespace(model='fake'),
                                    output_root=tmp_path, quiet=True)) == 0
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text())
    artifact = json.loads((folder / 'retrieval.json').read_text(encoding='utf-8'))
    assert report['model_calls'] == len(captured) == 1
    # 默认冻结语料为 v1：不追加 M5.2 契约键。
    assert set(artifact) == LEGACY_KEYS | FEATURE_KEYS
    assert KNOWLEDGE_KEYS.isdisjoint(artifact)
