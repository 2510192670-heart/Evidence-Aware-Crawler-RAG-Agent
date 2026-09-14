"""M5.0 contract lock for retrieval, the RAG toggle and the benchmark freeze.

M5.1 changed how cases are ranked and added feature result keys. These tests
still freeze what must NOT change: the legacy result keys, the RAG-off output,
the algorithm name and the frozen benchmark hash. The M5.1 migration is
additive only: legacy keys remain a compatibility subset and the feature keys
are appended as the M5.1 live contract. This file must not be weakened to let
anything beyond that through.
"""

from backend.app.rag import contracts as rag_contracts
from backend.app.rag.retrieval import retrieve
from benchmarks import HASH_PATH, content_sha256, load

SUMMARY = [{'request_id': 'r', 'method': 'GET', 'status': 200,
            'query': {'page': '1'},
            'response_shape': {'items': [{'id': '<integer>'}],
                               'has_next': '<boolean>', 'total': '<integer>'}}]

# Captured at the M4.4 freeze; changing these requires milestone approval.
FROZEN_BENCHMARK_SHA256 = '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'
FROZEN_CORPUS_VERSION = 1
CASE_KEYS = {'id', 'title', 'terms', 'guidance', 'score'}


def _is_hex_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in '0123456789abcdef' for char in value))


# --- retrieval result contract ------------------------------------------

def test_legacy_result_keys_are_present_with_the_frozen_types():
    result = retrieve(SUMMARY)
    assert set(rag_contracts.LEGACY_RESULT_KEYS).issubset(result)
    # M5.1-B 只允许 additive：新增键必须正好是声明的特征键。
    assert set(result) - set(rag_contracts.LEGACY_RESULT_KEYS) == set(rag_contracts.FEATURE_RESULT_KEYS)
    assert type(result['enabled']) is bool
    assert result['corpus_version'] == FROZEN_CORPUS_VERSION
    assert _is_hex_sha256(result['corpus_sha256'])
    assert isinstance(result['cases'], list)


def test_algorithm_name_is_frozen_to_bm25okapi():
    assert rag_contracts.ALGORITHM == 'BM25Okapi'
    assert retrieve(SUMMARY)['algorithm'] == rag_contracts.ALGORITHM


def test_case_entries_keep_the_legacy_shape():
    cases = retrieve(SUMMARY)['cases']
    assert cases, 'the frozen corpus must still match the canonical GET summary'
    for case in cases:
        assert set(case) == CASE_KEYS
        assert isinstance(case['terms'], list)
        assert type(case['score']) is float
    assert len(cases) <= 2


def test_rag_disabled_returns_the_frozen_empty_result():
    disabled = retrieve(SUMMARY, enabled=False)
    assert disabled['enabled'] is False
    assert disabled['algorithm'] == 'BM25Okapi'
    assert disabled['corpus_version'] == FROZEN_CORPUS_VERSION
    assert _is_hex_sha256(disabled['corpus_sha256'])
    assert disabled['cases'] == []


def test_rag_disabled_result_does_not_depend_on_the_summaries():
    # 关闭 RAG 时，输入摘要不得影响任何输出字段。
    assert retrieve([], enabled=False) == retrieve(SUMMARY, enabled=False)


def test_rag_toggle_only_flips_enabled_and_cases():
    enabled = retrieve(SUMMARY, enabled=True)
    disabled = retrieve(SUMMARY, enabled=False)
    assert enabled['enabled'] is True and disabled['enabled'] is False
    assert enabled['cases'] and disabled['cases'] == []
    # 关闭 RAG 的输出与 M4.4 冻结结果逐字段一致：只有 legacy 键。
    assert set(disabled) == set(rag_contracts.LEGACY_RESULT_KEYS)
    # 打开 RAG 后，除 enabled/cases 外 legacy 字段值不变，并只 additive 追加特征键。
    shared = {'enabled', 'cases'}
    assert ({key: value for key, value in enabled.items() if key not in shared
             and key in rag_contracts.LEGACY_RESULT_KEYS}
            == {key: value for key, value in disabled.items() if key not in shared})


# --- the M5.1 feature keys are produced additively ----------------------

def test_feature_result_keys_are_produced_additively_only_when_enabled():
    assert rag_contracts.FEATURE_SCHEMA_VERSION == 1
    assert set(rag_contracts.LEGACY_RESULT_KEYS).isdisjoint(rag_contracts.FEATURE_RESULT_KEYS)
    enabled = retrieve(SUMMARY)
    assert set(rag_contracts.FEATURE_RESULT_KEYS).issubset(enabled)
    assert set(retrieve(SUMMARY, enabled=False)) == set(rag_contracts.LEGACY_RESULT_KEYS)


# --- frozen benchmark ----------------------------------------------------

def test_benchmark_manifest_content_hash_is_frozen():
    assert content_sha256(load()) == FROZEN_BENCHMARK_SHA256


def test_benchmark_hash_file_is_frozen():
    assert HASH_PATH.read_text(encoding='utf-8').strip() == FROZEN_BENCHMARK_SHA256
