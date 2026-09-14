"""M5.1-B: deterministic, feature-aware retrieval on top of BM25.

BM25 still ranks by text relevance; evidence features only add a deterministic
safety gate and a light score adjustment. The frozen corpus has no ``features``,
so the legacy BM25 behaviour (and the RAG-off output) must stay byte-identical.
"""

import json
from pathlib import Path

from rank_bm25 import BM25Okapi

from benchmarks import HASH_PATH, content_sha256, load
from backend.app.rag import features, retrieval
from backend.app.rag.retrieval import retrieve

# 与 M5.0 冻结值一致；改变它需要里程碑批准。
FROZEN_BENCHMARK_SHA256 = '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'

LEGACY_KEYS = {'enabled', 'algorithm', 'corpus_version', 'corpus_sha256', 'cases'}
CASE_KEYS = {'id', 'title', 'terms', 'guidance', 'score'}
GATE_KEYS = {'applied', 'filtered', 'matched', 'conflicted'}

GET_SUMMARY = [{'request_id': 'r', 'method': 'GET', 'status': 200, 'query': {'page': '1'},
                'response_shape': {'items': [{'id': '<integer>'}],
                                   'has_next': '<boolean>', 'total': '<integer>'}}]
POST_SUMMARY = [{'request_id': 'r', 'method': 'POST', 'status': 200, 'query': {},
                 'response_shape': {'items': [{'id': '<integer>'}],
                                    'has_next': '<boolean>', 'total': '<integer>'},
                 'request_body_shape': {'page': 1}}]
# 无 method、无页码值 -> 除结构外的特征全部 unknown。
NO_METHOD_SUMMARY = [{'request_id': 'r', 'query': {},
                      'response_shape': {'items': [{'id': '<integer>'}],
                                         'has_next': '<boolean>', 'total': '<integer>'}}]

# 每个 case 只命中一个 query token（df=1），保证 BM25 分数为正且三者相等，
# 从而让排序差异只来自 feature gate。
TERMS_ITEMS = ['items', 'alpha_marker']
TERMS_TOTAL = ['total', 'beta_marker']
TERMS_HASNEXT = ['has_next', 'gamma_marker']


def case(case_id, features=None, terms=TERMS_HASNEXT):
    entry = {'id': case_id, 'title': case_id, 'terms': list(terms), 'guidance': ''}
    if features is not None:
        entry['features'] = features
    return entry


def ids(result):
    return [entry['id'] for entry in result['cases']]


# --- 1. legacy BM25 cases without features still retrieve ----------------

def test_legacy_cases_without_features_still_retrieve():
    result = retrieve(GET_SUMMARY)
    assert result['cases'], 'the frozen BM25 corpus must still match the canonical GET summary'
    assert result['cases'][0]['id'] == 'page_items'
    for entry in result['cases']:
        assert set(entry) == CASE_KEYS
        assert type(entry['score']) is float
    # 语料没有 features -> gate 未生效。
    assert result['gate'] == {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}


# --- 2. a POST json_body query prefers the POST case ---------------------

def test_post_json_body_query_prefers_feature_matching_case():
    corpus = [case('get_case', {'method': 'GET', 'page_location': 'query'}, TERMS_TOTAL),
              case('post_case', {'method': 'POST', 'page_location': 'json_body'}, TERMS_ITEMS),
              case('plain_case', None, TERMS_HASNEXT)]
    result = retrieve(POST_SUMMARY, cases=corpus)
    assert ids(result)[0] == 'post_case'
    assert 'plain_case' in ids(result)
    # 相反的 method 属于安全边界，直接过滤而不是降权。
    assert 'get_case' in result['gate']['filtered']
    assert 'get_case' not in ids(result)
    # 即使 case 声明了 features，结果条目仍保持 legacy 形状。
    assert set(result['cases'][0]) == CASE_KEYS


# --- 3. a GET query is never overtaken by a POST case --------------------

def test_get_query_is_not_overtaken_by_post_case():
    corpus = [case('post_case', {'method': 'POST', 'page_location': 'json_body'}, TERMS_ITEMS),
              case('get_case', {'method': 'GET', 'page_location': 'query'}, TERMS_TOTAL),
              case('plain_case', None, TERMS_HASNEXT)]
    result = retrieve(GET_SUMMARY, cases=corpus)
    assert 'post_case' in result['gate']['filtered']
    assert 'post_case' not in ids(result)
    assert ids(result)[0] == 'get_case'


# --- 4. unknown features neither filter nor adjust -----------------------

def test_unknown_query_features_do_not_change_results():
    with_features = [case('get_case', {'method': 'GET', 'page_location': 'query'}, TERMS_TOTAL),
                     case('post_case', {'method': 'POST', 'page_location': 'json_body'}, TERMS_ITEMS),
                     case('plain_case', None, TERMS_HASNEXT)]
    stripped = [case('get_case', None, TERMS_TOTAL),
                case('post_case', None, TERMS_ITEMS),
                case('plain_case', None, TERMS_HASNEXT)]
    annotated = retrieve(NO_METHOD_SUMMARY, cases=with_features)
    plain = retrieve(NO_METHOD_SUMMARY, cases=stripped)
    assert annotated['cases'] == plain['cases']
    assert annotated['gate'] == {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}


# --- 5. a non-boundary conflict downweights (but keeps) the case ---------

def test_feature_conflict_downweights_without_filtering():
    corpus = [case('match_case', {'method': 'GET', 'page_location': 'query'}, TERMS_ITEMS),
              case('conflict_case', {'method': 'GET', 'page_location': 'json_body'}, TERMS_TOTAL),
              case('neutral_case', None, TERMS_HASNEXT)]
    result = retrieve(GET_SUMMARY, cases=corpus)
    assert ids(result) == ['match_case', 'conflict_case']
    assert 'conflict_case' in result['gate']['conflicted']
    assert 'conflict_case' not in result['gate']['filtered']
    scores = {entry['id']: entry['score'] for entry in result['cases']}
    assert scores['match_case'] > scores['conflict_case']


# --- 6. the BM25 score is still the ranking core -------------------------

def test_bm25_score_is_still_present_and_bm25_based():
    cases = json.loads(Path(retrieval.__file__).with_name('cases.json').read_bytes())
    query = sorted({token for summary in GET_SUMMARY for section in ('query', 'response_shape')
                    for token in retrieval.structure_keys(summary.get(section, {}))})
    corpus = [retrieval.tokens(' '.join(entry['terms'])) for entry in cases]
    raw = BM25Okapi(corpus).get_scores(query)
    result = retrieve(GET_SUMMARY)
    for entry in result['cases']:
        assert type(entry['score']) is float and entry['score'] > 0
    top = result['cases'][0]
    index = next(i for i, entry in enumerate(cases) if entry['id'] == top['id'])
    # 冻结语料没有 features -> 返回的分数必须等于原始 BM25 分数。
    assert top['score'] == round(float(raw[index]), 6)


# --- 7. RAG disabled output is completely unchanged ----------------------

def test_rag_disabled_output_is_unchanged():
    disabled = retrieve(POST_SUMMARY, enabled=False)
    assert set(disabled) == LEGACY_KEYS
    assert disabled['enabled'] is False
    assert disabled['algorithm'] == 'BM25Okapi'
    assert disabled['corpus_version'] == 1
    assert len(disabled['corpus_sha256']) == 64
    assert disabled['cases'] == []
    assert disabled == retrieve([], enabled=False)


# --- 8. new result fields are additive only ------------------------------

def test_new_result_fields_are_additive():
    result = retrieve(GET_SUMMARY)
    assert LEGACY_KEYS.issubset(result)
    assert set(result) == LEGACY_KEYS | {'feature_schema_version', 'query_features', 'gate'}
    assert result['feature_schema_version'] == 1
    assert set(result['query_features']) == set(features.FEATURE_KEYS)
    assert set(result['gate']) == GATE_KEYS


# --- 9. identical input yields identical output --------------------------

def test_same_input_is_deterministic():
    corpus = [case('get_case', {'method': 'GET', 'page_location': 'query'}, TERMS_TOTAL),
              case('post_case', {'method': 'POST', 'page_location': 'json_body'}, TERMS_ITEMS),
              case('plain_case', None, TERMS_HASNEXT)]
    first = retrieve(POST_SUMMARY, cases=corpus)
    second = retrieve(POST_SUMMARY, cases=corpus)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- 10. the frozen benchmark hash is unchanged --------------------------

def test_benchmark_hash_is_unchanged():
    assert content_sha256(load()) == FROZEN_BENCHMARK_SHA256
    assert HASH_PATH.read_text(encoding='utf-8').strip() == FROZEN_BENCHMARK_SHA256
