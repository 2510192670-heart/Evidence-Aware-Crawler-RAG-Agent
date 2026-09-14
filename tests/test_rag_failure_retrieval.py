"""M5.2-B: failure-aware knowledge ranking layered on BM25 + feature gate.

Covers legacy compatibility, v2 failure matching (exact code / category and phase
penalties), the restricted-category safety boundary, unknown-code neutrality, the
disabled-RAG contract, no second model call, determinism and frozen-input guards.
The evaluation at the end is a small in-repo fixture; it does not touch the
frozen benchmark.
"""

import ast
import hashlib
import json
from pathlib import Path

import pytest

from benchmarks import HASH_PATH, content_sha256, load
from backend.app.rag import knowledge, retrieval
from backend.app.rag.retrieval import CONFLICT, FAILURE_MATCH_BONUS, IGNORED, MATCH, failure_match, retrieve

# Captured at freeze time; changing these requires milestone approval.
FROZEN_BENCHMARK_SHA256 = '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'
FROZEN_CASES_SHA256 = '678f87ece8bdb0aa4171f5f80b6b289cd773215639b7e30d82dc4953f5cba1f2'
LEGACY_KEYS = {'enabled', 'algorithm', 'corpus_version', 'corpus_sha256', 'cases'}
FEATURE_KEYS = {'feature_schema_version', 'query_features', 'gate'}
KNOWLEDGE_KEYS = {'case_schema_version', 'knowledge_gate'}
CASE_KEYS = {'id', 'title', 'terms', 'guidance', 'score'}

GET_SUMMARY = [{'request_id': 'r', 'method': 'GET', 'status': 200, 'query': {'page': '1'},
                'response_shape': {'items': [{'id': '<integer>'}],
                                   'has_next': '<boolean>', 'total': '<integer>'}}]

# 每个 case 只命中一个 query token（df=1），BM25 分数相等，排序差异只来自确定性层。
POINTER_NOT_FOUND = {'error_code': 'pointer_not_found'}
INVALID_TOTAL_POINTER = {'error_code': 'invalid_total_pointer'}
SECURITY_FAILURE = {'error_code': 'invalid_url'}


def corpus():
    return [
        {'id': 'acase', 'title': 'total pointer 失败知识', 'terms': ['total', 'am'], 'guidance': 'g',
         'features': {'method': 'GET', 'page_location': 'query'},
         'failure_modes': [{'code': 'invalid_total_pointer', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'total_not_int'}]},
        {'id': 'plain', 'title': 'legacy 案例', 'terms': ['has_next', 'pm'], 'guidance': 'g'},
        {'id': 'zcase', 'title': 'auto 可修复失败知识', 'terms': ['items', 'zm'], 'guidance': 'g',
         'features': {'method': 'GET', 'page_location': 'query'},
         'failure_modes': [{'code': 'pointer_not_found', 'category': 'PLAN_SEMANTIC',
                            'phase': 'analyzing', 'signal': 'pointer_absent'}],
         'repairability': {'pointer_not_found': 'auto_applied'}},
    ]


def _strip(items, keys):
    return [{key: value for key, value in item.items() if key not in keys} for item in items]


def ids(result):
    return [entry['id'] for entry in result['cases']]


def known(case_id):
    return {case.id: case for case in knowledge.load(corpus())}[case_id]


# --- 1. legacy case retrieval is unchanged -------------------------------

def test_legacy_corpus_retrieval_is_unchanged():
    result = retrieve(GET_SUMMARY)
    assert set(result) == LEGACY_KEYS | FEATURE_KEYS          # failure-aware keys absent
    assert ids(result)[0] == 'page_items'
    assert result == retrieve(GET_SUMMARY)
    for entry in result['cases']:
        assert set(entry) == CASE_KEYS


# --- 2. a v2 failure match increases ranking -----------------------------

def test_failure_match_increases_ranking():
    baseline = retrieve(GET_SUMMARY, cases=corpus())
    gated = retrieve(GET_SUMMARY, cases=corpus(), failure_context=POINTER_NOT_FOUND)
    assert ids(baseline)[0] == 'acase'      # no failure context -> neutral, id-ordered
    assert ids(gated)[0] == 'zcase'         # exact error_code match wins
    scored_baseline = {entry['id']: entry['score'] for entry in baseline['cases']}
    scored_gated = {entry['id']: entry['score'] for entry in gated['cases']}
    assert scored_gated['zcase'] - scored_baseline['zcase'] == pytest.approx(3 * FAILURE_MATCH_BONUS)
    assert 'zcase' in gated['knowledge_gate']['matched']
    assert gated['case_schema_version'] == 2


# --- 3. exact error code match -------------------------------------------

def test_exact_error_code_match():
    matched = failure_match({'failure_context': POINTER_NOT_FOUND}, known('zcase'))
    assert matched['verdicts']['error_code'] == MATCH
    unmatched = failure_match({'failure_context': POINTER_NOT_FOUND}, known('acase'))
    assert unmatched['verdicts']['error_code'] == IGNORED   # 非 declare 的码是 neutral，不是 conflict


# --- 4. category mismatch penalty ----------------------------------------

def test_category_mismatch_penalizes():
    case = knowledge.validate_case({
        'id': 'n', 'title': 't', 'terms': [], 'guidance': '',
        'failure_modes': [{'code': 'network_error', 'category': 'TRANSPORT', 'phase': 'executing',
                           'signal': 'transient'}]})
    result = failure_match({'failure_context': POINTER_NOT_FOUND}, case)
    assert result['verdicts']['category'] == CONFLICT
    assert result['adjustment'] < 0


# --- 5. phase mismatch penalty -------------------------------------------

def test_phase_mismatch_penalizes():
    case = knowledge.validate_case({
        'id': 's', 'title': 't', 'terms': [], 'guidance': '',
        'failure_modes': [{'code': 'sensitive_pointer', 'category': 'SECURITY', 'phase': 'analyzing',
                           'signal': 'sensitive_key'}]})
    # invalid_url 是 SECURITY/observing，与案例的 SECURITY/analyzing 同类别不同阶段。
    result = failure_match({'failure_context': SECURITY_FAILURE}, case)
    assert result['verdicts']['category'] == MATCH
    assert result['verdicts']['phase'] == CONFLICT
    assert result['filtered'] is False


# --- 6. SECURITY never retrieves auto-repair guidance --------------------

def test_restricted_failure_never_recommends_auto_applied_case():
    gated = retrieve(GET_SUMMARY, cases=corpus(), failure_context=SECURITY_FAILURE)
    assert 'zcase' in gated['knowledge_gate']['filtered']
    assert 'zcase' not in ids(gated)
    # 结果中不得有任何声明 auto_applied 的案例。
    auto_ids = {case.id for case in knowledge.load(corpus())
                if knowledge.AUTO_APPLIED in case.repairability.values()}
    assert auto_ids and auto_ids.isdisjoint(ids(gated))


# --- 7. unknown failure code is neutral ----------------------------------

def test_unknown_failure_code_is_neutral():
    neutral = retrieve(GET_SUMMARY, cases=corpus(), failure_context={'error_code': 'not_registered'})
    baseline = retrieve(GET_SUMMARY, cases=corpus())
    assert neutral['cases'] == baseline['cases']
    assert neutral['knowledge_gate'] == {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}


# --- 8. disabled RAG is unchanged ----------------------------------------

def test_disabled_rag_is_unchanged():
    disabled = retrieve(GET_SUMMARY, enabled=False)
    assert set(disabled) == LEGACY_KEYS
    assert disabled['cases'] == []
    assert disabled == retrieve([], enabled=False)
    assert disabled == retrieve(GET_SUMMARY, enabled=False, failure_context=POINTER_NOT_FOUND)


# --- 9. no second model call ---------------------------------------------

def test_retrieval_introduces_no_model_call():
    tree = ast.parse(Path(retrieval.__file__).read_text(encoding='utf-8'))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or '')
    joined = ' '.join(modules)
    for forbidden in ('llm', 'gateway', 'openai', 'httpx', 'requests', 'execution'):
        assert forbidden not in joined, forbidden
    # 检索是纯函数：不需要任何模型凭证即可运行。
    assert retrieve(GET_SUMMARY, cases=corpus(), failure_context=POINTER_NOT_FOUND)['cases']


# --- 10. deterministic output --------------------------------------------

def test_same_input_is_deterministic():
    first = retrieve(GET_SUMMARY, cases=corpus(), failure_context=POINTER_NOT_FOUND)
    second = retrieve(GET_SUMMARY, cases=corpus(), failure_context=POINTER_NOT_FOUND)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- 11. frozen benchmark hash unchanged ---------------------------------

def test_benchmark_hash_is_unchanged():
    assert content_sha256(load()) == FROZEN_BENCHMARK_SHA256
    assert HASH_PATH.read_text(encoding='utf-8').strip() == FROZEN_BENCHMARK_SHA256


# --- 12. the frozen cases.json is unchanged ------------------------------

def test_frozen_cases_json_is_unchanged():
    raw = Path(knowledge.__file__).with_name('cases.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    assert all(case.case_schema_version == 1 for case in knowledge.load())


# --- evaluation: BM25 vs feature gate vs failure-aware (fixture only) -----

EVAL_QUERIES = [
    {'expected': 'zcase', 'failure_context': POINTER_NOT_FOUND},
    {'expected': 'acase', 'failure_context': INVALID_TOTAL_POINTER},
]


def _metrics(groups):
    out = {}
    for name, corpus_cases in groups.items():
        top1 = top2 = mrr = 0.0
        for query in EVAL_QUERIES:
            ranked = ids(retrieve(GET_SUMMARY, cases=corpus_cases,
                                  failure_context=query['failure_context']))
            expected = query['expected']
            top1 += 1.0 if ranked and ranked[0] == expected else 0.0
            top2 += 1.0 if expected in ranked else 0.0
            mrr += 1.0 / (ranked.index(expected) + 1) if expected in ranked else 0.0
        total = len(EVAL_QUERIES)
        out[name] = {'top1_hit': round(top1 / total, 6), 'top2_hit': round(top2 / total, 6),
                     'mrr': round(mrr / total, 6)}
    return out


def test_three_group_evaluation_is_deterministic():
    groups = {'bm25': _strip(corpus(), ('features', 'failure_modes', 'repairability')),
              'feature_gate': _strip(corpus(), ('failure_modes', 'repairability')),
              'failure_aware': corpus()}
    first = _metrics(groups)
    assert first == _metrics(groups)
    assert set(first) == {'bm25', 'feature_gate', 'failure_aware'}
    for metrics in first.values():
        assert set(metrics) == {'top1_hit', 'top2_hit', 'mrr'}
    # 确定性消融：只在 testing fixture 内完成，不改动 benchmark。
    assert first['bm25'] == {'top1_hit': 0.5, 'top2_hit': 0.5, 'mrr': 0.5}
    assert first['feature_gate'] == {'top1_hit': 0.5, 'top2_hit': 1.0, 'mrr': 0.75}
    assert first['failure_aware'] == {'top1_hit': 1.0, 'top2_hit': 1.0, 'mrr': 1.0}
    # 只有含结构化知识的组才追加 M5.2 契约键。
    assert set(retrieve(GET_SUMMARY, cases=groups['bm25'])) == LEGACY_KEYS | FEATURE_KEYS
    assert set(retrieve(GET_SUMMARY, cases=groups['failure_aware'])) == LEGACY_KEYS | FEATURE_KEYS | KNOWLEDGE_KEYS
