"""M5.1-C deterministic retrieval evaluation.

Compares two ablations over one eval-only corpus:

  A (baseline)      BM25 only          -- the same cases with ``features`` removed
  B (feature_gate)  BM25 + feature gate -- the cases as declared

The two corpora differ only by ``features``, so BM25 scores are identical and
any ranking difference is attributable to the deterministic gate. The frozen
retrieval corpus (``cases.json``) and the benchmark manifest are never touched:
the fixture lives here.

The goal is NOT to prove the gate is stronger. It is to show the gate is
deterministic, explainable, and inert when no case declares features (so the
legacy BM25 behaviour cannot change).
"""

import copy

from benchmarks import HASH_PATH, content_sha256, load
from backend.app.rag.retrieval import retrieve

# 与 M5.0 冻结值一致；本评估不得触碰 benchmark。
FROZEN_BENCHMARK_SHA256 = '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'

EMPTY_GATE = {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}
RESULT_KEYS = {'id', 'title', 'terms', 'guidance', 'score'}

# 评估专用语料（不写入 benchmarks/，也不修改 cases.json）。除 features 外，
# A/B 两组的 terms/id 完全一致，因此 BM25 分数一致，差异只可能来自 gate。
EVAL_CASES = [
    {'id': 'get_page', 'title': 'GET 查询参数翻页', 'terms': ['page', 'items', 'has_next', 'total'],
     'guidance': '', 'features': {'method': 'GET', 'page_location': 'query', 'items_container': 'nested',
                                  'stop_signal': 'boolean_flag', 'continuation_flag': 'present'}},
    {'id': 'post_page', 'title': 'POST JSON body 翻页', 'terms': ['page', 'items', 'id', 'has_next', 'total'],
     'guidance': '', 'features': {'method': 'POST', 'page_location': 'json_body', 'items_container': 'nested',
                                  'stop_signal': 'boolean_flag', 'continuation_flag': 'present'}},
    {'id': 'root_list', 'title': '顶层列表 + 仅总数', 'terms': ['items', 'id', 'title'],
     'guidance': '', 'features': {'items_container': 'root_list', 'stop_signal': 'total_int_only',
                                  'continuation_flag': 'absent'}},
    {'id': 'nested_records', 'title': '嵌套 data.records',
     'terms': ['pageNo', 'data', 'records', 'totalCount', 'hasMore'], 'guidance': '',
     'features': {'method': 'GET', 'page_location': 'query', 'items_container': 'nested',
                  'stop_signal': 'boolean_flag', 'continuation_flag': 'present'}},
    {'id': 'aliased_rows', 'title': '别名字段映射', 'terms': ['p', 'rows', 'sku', 'title', 'more', 'total'],
     'guidance': '', 'features': {'method': 'GET', 'page_location': 'query', 'items_container': 'nested',
                                  'field_mapping': 'aliased_required'}},
    {'id': 'total_pages_only', 'title': '只有总页数', 'terms': ['pageIndex', 'list', 'count', 'totalPages'],
     'guidance': '', 'features': {'method': 'GET', 'page_location': 'query',
                                  'stop_signal': 'total_pages_ambiguous', 'continuation_flag': 'absent'}},
    {'id': 'cursor_unsupported', 'title': '游标分页边界',
     'terms': ['cursor', 'next_cursor', 'edges', 'pageInfo', 'endCursor'], 'guidance': '',
     'features': {'stop_signal': 'none'}},
    {'id': 'offset_unsupported', 'title': '偏移量分页边界',
     'terms': ['offset', 'limit', 'results', 'nextOffset'], 'guidance': '',
     'features': {'method': 'GET', 'page_location': 'query', 'stop_signal': 'none'}},
]

QUERIES = [
    {'name': 'get_query_page', 'expected': 'get_page',
     'summary': {'method': 'GET', 'query': {'page': '1'},
                 'response_shape': {'items': [{'id': '<integer>'}],
                                    'has_next': '<boolean>', 'total': '<integer>'}}},
    {'name': 'post_json_page', 'expected': 'post_page',
     'summary': {'method': 'POST', 'query': {},
                 'response_shape': {'items': [{'id': '<integer>'}],
                                    'has_next': '<boolean>', 'total': '<integer>'},
                 'request_body_shape': {'page': 1}}},
    {'name': 'root_list_page', 'expected': 'root_list',
     'summary': {'method': 'GET', 'query': {'page': '1'},
                 'response_shape': [{'id': '<integer>', 'title': '<string>'}]}},
    {'name': 'nested_records_page', 'expected': 'nested_records',
     'summary': {'method': 'GET', 'query': {'pageNo': '1'},
                 'response_shape': {'data': {'records': [{'code': '<string>'}],
                                             'totalCount': '<integer>', 'hasMore': '<boolean>'}}}},
    {'name': 'cursor_page', 'expected': 'cursor_unsupported',
     'summary': {'method': 'GET', 'query': {'cursor': 'abc'},
                 'response_shape': {'edges': [{'node': {'id': '<integer>'}}],
                                    'pageInfo': {'endCursor': '<string>', 'hasNextPage': '<boolean>'}}}},
    {'name': 'offset_page', 'expected': 'offset_unsupported',
     'summary': {'method': 'GET', 'query': {'offset': '20', 'limit': '10'},
                 'response_shape': {'results': [{'id': '<integer>'}], 'nextOffset': '<integer>'}}},
]


def _without_features(cases):
    return [{key: value for key, value in case.items() if key != 'features'} for case in cases]


def _ranked_ids(result):
    return [entry['id'] for entry in result['cases']]


def _reciprocal_rank(ids, expected):
    return 1.0 / (ids.index(expected) + 1) if expected in ids else 0.0


def _metrics(rows, group):
    top1 = top2 = mrr = 0.0
    for row in rows:
        ids = _ranked_ids(row[group])
        expected = row['expected']
        top1 += 1.0 if ids and ids[0] == expected else 0.0
        top2 += 1.0 if expected in ids else 0.0
        mrr += _reciprocal_rank(ids, expected)
    total = len(rows)
    return {'top1_hit': round(top1 / total, 6), 'top2_hit': round(top2 / total, 6),
            'mrr': round(mrr / total, 6)}


def evaluate(corpus=EVAL_CASES):
    """Return baseline vs feature-gate retrieval results and metrics.

    Deterministic: the corpora and queries are fixed and the gate is a pure
    function of evidence features, so identical input yields identical output.
    """
    feature_cases = copy.deepcopy(corpus)
    baseline_cases = _without_features(copy.deepcopy(corpus))
    rows = []
    for query in QUERIES:
        rows.append({'name': query['name'], 'expected': query['expected'],
                     'baseline': retrieve([query['summary']], cases=baseline_cases),
                     'feature_gate': retrieve([query['summary']], cases=feature_cases)})
    return {'baseline': _metrics(rows, 'baseline'),
            'feature_gate': _metrics(rows, 'feature_gate'), 'queries': rows}


# --- metrics are present, complete and deterministic ---------------------

def test_metrics_are_complete_and_deterministic():
    first = evaluate()
    second = evaluate()
    assert first == second
    for group in ('baseline', 'feature_gate'):
        metrics = first[group]
        assert set(metrics) == {'top1_hit', 'top2_hit', 'mrr'}
        for value in metrics.values():
            assert type(value) is float and 0.0 <= value <= 1.0
    assert len(first['queries']) == len(QUERIES)


# --- the baseline group is pure BM25 (gate inactive) ---------------------

def test_baseline_group_runs_with_the_gate_inactive():
    for row in evaluate()['queries']:
        baseline = row['baseline']
        # 无 features -> gate 不生效，A 组就是纯 BM25。
        assert baseline['gate'] == EMPTY_GATE
        # 两组 query_features 来自同一摘要、同一抽取器，必须一致。
        assert baseline['query_features'] == row['feature_gate']['query_features']
        for entry in baseline['cases']:
            assert set(entry) == RESULT_KEYS


# --- every change is deterministic and explainable -----------------------

def test_feature_gate_changes_are_explainable():
    for row in evaluate()['queries']:
        baseline, gated = row['baseline'], row['feature_gate']
        gate = gated['gate']
        a_scores = {entry['id']: entry['score'] for entry in baseline['cases']}
        b_scores = {entry['id']: entry['score'] for entry in gated['cases']}
        # 结果里消失的 case 必须正好是被安全边界过滤的候选。
        assert set(a_scores) - set(b_scores) <= set(gate['filtered'])
        # 被过滤的 case 绝不出现在结果中。
        assert not (set(gate['filtered']) & set(b_scores))
        # 分数变化必须由 matched/conflicted 解释；未涉及的 case 分数不变。
        for case_id in set(a_scores) & set(b_scores):
            matched = case_id in gate['matched']
            conflicted = case_id in gate['conflicted']
            if matched and not conflicted:
                assert b_scores[case_id] >= a_scores[case_id]
            elif conflicted and not matched:
                assert b_scores[case_id] <= a_scores[case_id]
            elif not matched and not conflicted:
                assert b_scores[case_id] == a_scores[case_id]


def test_method_boundary_filter_is_the_only_top1_change():
    rows = {row['name']: row for row in evaluate()['queries']}
    changed = [name for name, row in rows.items()
               if _ranked_ids(row['baseline'])[:1] != _ranked_ids(row['feature_gate'])[:1]]
    # 唯一的 top1 变化来自 GET 查询，BM25 把 POST case 排在首位，gate 按 method
    # 安全边界过滤它，从而让 GET case 回到首位。
    assert changed == ['get_query_page']
    row = rows['get_query_page']
    assert _ranked_ids(row['baseline'])[0] == 'post_page'
    assert _ranked_ids(row['feature_gate'])[0] == 'get_page'
    assert 'post_page' in row['feature_gate']['gate']['filtered']
    assert 'get_page' in row['feature_gate']['gate']['matched']


# --- non-regression: the gate is inert on the frozen corpus --------------

def test_frozen_corpus_gate_is_inactive_and_does_not_change_bm25():
    for query in QUERIES:
        result = retrieve([query['summary']])
        # 冻结语料没有 features -> gate 从不生效，旧 BM25 行为不变。
        assert result['gate'] == EMPTY_GATE
        for entry in result['cases']:
            assert set(entry) == RESULT_KEYS


# --- the evaluation must not pollute the frozen benchmark ----------------

def test_evaluation_does_not_touch_the_frozen_benchmark():
    assert content_sha256(load()) == FROZEN_BENCHMARK_SHA256
    assert HASH_PATH.read_text(encoding='utf-8').strip() == FROZEN_BENCHMARK_SHA256
    # 评估语料是独立 fixture：id 唯一，且不读取 benchmark manifest。
    assert len(EVAL_CASES) == len({case['id'] for case in EVAL_CASES})
