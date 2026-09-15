"""M6.1-B2 deterministic semantic overlap detector.

Offline and model-free. Every expectation is derived from data the detector hashes,
never from a copied fixture and never from a case-id shortcut. The known A
regression is read straight from the frozen ``evaluation/datasets/m6-v1`` bundle.
"""
import ast
import json
import random
from pathlib import Path

import pytest

from evaluation.overlap import (S1_SAME_FAMILY_ID, S2_EXACT_STRUCTURAL_SIGNATURE,
                                S3_EXACT_ORACLE_RESULT_HASH, S4_EXACT_RECORD_SET,
                                S5_EXACT_RESPONSE_PAYLOAD, W1_PARTIAL_RECORD_OVERLAP,
                                W2_NESTED_RESPONSE_SUBTREE_OVERLAP, W3_ENDPOINT_COMPOSITION_MATCH,
                                W4_PAGINATION_SEMANTICS_MATCH, W5_FAILURE_CODE_MATCH,
                                W6_REPAIR_DISPOSITION_MATCH, NormalizedView, OverlapDecision,
                                audit_cross_split, compare, load_m6_v1_views, normalize_case)
from evaluation.schemas.contract_v2 import assert_m61a_frozen

ROOT = Path(__file__).resolve().parents[1]
OVERLAP_SRC = ROOT / 'evaluation' / 'overlap.py'
CANARY = 'M6_OVERLAP_SECRET_CANARY_7f91'
SHA_A = 'a' * 64
SHA_B = 'b' * 64


def assert_real_bundle_frozen():
    """Contract-B precondition for the projection-only loader.

    ``load_m6_v1_views`` only projects data; the caller must first prove the bundle
    is frozen. ``assert_m61a_frozen`` is the sole existing freeze guard, so this
    helper delegates to it and never re-implements its hashing/pinning logic. It is
    invoked explicitly by the real-bundle regression tests, never autouse.
    """
    assert assert_m61a_frozen(ROOT) == 12


# --------------------------------------------------------------------------
# Builders. Nothing here reads the dataset; the known-regression test does that.
# --------------------------------------------------------------------------
def ep(method='GET', query=None, body=None, responses=None):
    return {'method': method, 'query': {'page': '1'} if query is None else query,
            'request_body': body, 'responses': [{'items': []}] if responses is None else responses}


def fixture(*endpoints):
    return {'endpoints': list(endpoints)}


def make_view(case_id, split, *, family_id=None, family_signature=None, records=None,
              result_sha256=None, expected_error_code=None, disposition='none', endpoints=None):
    family_id = family_id if family_id is not None else 'fam_' + case_id
    family_signature = {'sig': family_id} if family_signature is None else family_signature
    if endpoints is None:
        endpoints = [ep(responses=[{'items': [{'id': 0, 'value': case_id}], 'more': False, 'total': 0}])]
    case_raw = {'id': case_id, 'split': split, 'family_id': family_id,
                'repair_expectation': {'policy_disposition': disposition}}
    oracle_raw = {'records': records, 'result_sha256': result_sha256,
                  'expected_error_code': expected_error_code}
    return normalize_case(case_raw, oracle_raw, fixture(*endpoints), family_signature)


def blank_view(case_id, split, **over):
    """A view engineered to share nothing with another blank_view by default."""
    kwargs = dict(family_id='fam_' + case_id, family_signature={'sig': case_id},
                  records=[{'id': 1, 'value': case_id}],
                  endpoints=[ep(query={'page': '1'},
                                responses=[{'items': [{'id': 1, 'value': case_id}], 'more': True,
                                            'total': 1}])])
    kwargs.update(over)
    return make_view(case_id, split, **kwargs)


# --------------------------------------------------------------------------
# 1. no overlap -> AUTO_PASS
# --------------------------------------------------------------------------
def test_no_overlap_auto_passes():
    left = make_view('a_dev', 'development', family_id='fam_l', family_signature={'s': 'L'},
                     records=[{'id': 1, 'value': 'L'}],
                     endpoints=[ep(query={'page': '1'},
                                   responses=[{'items': [{'id': 1, 'value': 'L'}], 'more': True,
                                               'total': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', family_id='fam_r', family_signature={'s': 'R'},
                      records=[{'id': 2, 'value': 'R'}],
                      endpoints=[ep(method='POST', query={}, body={'offset': '0'},
                                    responses=[{'rows': [{'id': 2, 'value': 'R'}], 'count': 2}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.AUTO_PASS
    assert result.strong_signals == () and result.weak_signals == ()
    assert result.to_jsonable()['evidence'] == {}


# --------------------------------------------------------------------------
# 2-5, 7. strong signals -> BLOCK
# --------------------------------------------------------------------------
def test_same_family_id_blocks():
    result = compare(make_view('a_dev', 'development', family_id='shared_family'),
                     make_view('b_eval', 'frozen_evaluation', family_id='shared_family'))
    assert result.decision is OverlapDecision.BLOCK
    assert S1_SAME_FAMILY_ID in result.strong_signals


def test_exact_structural_signature_blocks_without_same_family():
    signature = {'endpoints': [{'method': 'GET', 'paging': 'page_number'}]}
    left = make_view('a_dev', 'development', family_id='fam_l', family_signature=signature)
    right = make_view('b_eval', 'frozen_evaluation', family_id='fam_r', family_signature=signature)
    result = compare(left, right)
    assert result.decision is OverlapDecision.BLOCK
    assert S2_EXACT_STRUCTURAL_SIGNATURE in result.strong_signals
    assert S1_SAME_FAMILY_ID not in result.strong_signals


def test_same_oracle_result_hash_blocks():
    left = make_view('a_dev', 'development', result_sha256=SHA_A)
    right = make_view('b_eval', 'frozen_evaluation', result_sha256=SHA_A)
    result = compare(left, right)
    assert result.decision is OverlapDecision.BLOCK
    assert S3_EXACT_ORACLE_RESULT_HASH in result.strong_signals
    assert result.to_jsonable()['evidence'][S3_EXACT_ORACLE_RESULT_HASH]['digest'] == SHA_A


def test_exact_non_empty_record_set_blocks():
    records = [{'id': 1, 'value': 'alpha'}, {'id': 2, 'value': 'beta'}]
    result = compare(make_view('a_dev', 'development', records=records),
                     make_view('b_eval', 'frozen_evaluation', records=records))
    assert result.decision is OverlapDecision.BLOCK
    assert S4_EXACT_RECORD_SET in result.strong_signals


def test_exact_endpoint_root_payload_blocks():
    payload = [{'items': [{'id': 1, 'value': 'x'}], 'total': 1}]
    left = make_view('a_dev', 'development', endpoints=[ep(responses=payload)])
    right = make_view('b_eval', 'frozen_evaluation', endpoints=[ep(responses=payload)])
    result = compare(left, right)
    assert result.decision is OverlapDecision.BLOCK
    assert S5_EXACT_RESPONSE_PAYLOAD in result.strong_signals


def test_strong_plus_weak_still_blocks():
    left = make_view('a_dev', 'development', family_id='shared',
                     endpoints=[ep(responses=[{'items': [{'id': 1, 'value': 'L'}], 'total': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', family_id='shared',
                      endpoints=[ep(responses=[{'items': [{'id': 2, 'value': 'R'}], 'total': 2}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.BLOCK
    assert S1_SAME_FAMILY_ID in result.strong_signals


# --------------------------------------------------------------------------
# 6, 8-12. single weak signal -> MANUAL_REVIEW (isolated)
# --------------------------------------------------------------------------
def test_partial_record_overlap_is_manual_review_only():
    left = make_view('a_dev', 'development', records=[{'id': 1, 'value': 'a'}, {'id': 2, 'value': 'b'}],
                     endpoints=[ep(query={'page': '1'}, responses=[{'x': 1}])])
    right = make_view('b_eval', 'frozen_evaluation',
                      records=[{'id': 2, 'value': 'b'}, {'id': 3, 'value': 'c'}],
                      endpoints=[ep(query={}, responses=[{'y': 'z'}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.MANUAL_REVIEW
    assert result.strong_signals == ()
    assert result.weak_signals == (W1_PARTIAL_RECORD_OVERLAP,)
    ev = result.to_jsonable()['evidence'][W1_PARTIAL_RECORD_OVERLAP]
    assert ev['shared_count'] == 1 and ev['left_count'] == 2 and ev['right_count'] == 2


def test_nested_subtree_only_is_manual_review():
    left = make_view('a_dev', 'development',
                     endpoints=[ep(query={'page': '1'},
                                   responses=[{'items': [{'id': 1, 'value': 'shared'}], 'more': True}])])
    right = make_view('b_eval', 'frozen_evaluation',
                      endpoints=[ep(query={'page': '1'},
                                    responses=[{'data': [{'id': 1, 'value': 'shared'}], 'total': 9}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.MANUAL_REVIEW
    assert result.strong_signals == ()
    assert result.weak_signals == (W2_NESTED_RESPONSE_SUBTREE_OVERLAP,)
    assert S5_EXACT_RESPONSE_PAYLOAD not in result.strong_signals


def test_endpoint_composition_only_is_manual_review():
    # Same composition (GET/query/object+array+int) but a different page key kind, so
    # the pagination signature differs and only W3 fires.
    left = make_view('a_dev', 'development',
                     endpoints=[ep(query={'page': '1'},
                                   responses=[{'items': [{'id': 1, 'value': 'L'}], 'total': 1}])])
    right = make_view('b_eval', 'frozen_evaluation',
                      endpoints=[ep(query={'cursor': 'x'},
                                    responses=[{'items': [{'id': 2, 'value': 'R'}], 'total': 2}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.MANUAL_REVIEW
    assert result.strong_signals == ()
    assert result.weak_signals == (W3_ENDPOINT_COMPOSITION_MATCH,)


def test_pagination_only_is_manual_review_and_never_blocks():
    # Same pagination semantics, different response container shape -> only W4.
    left = make_view('a_dev', 'development',
                     endpoints=[ep(query={'page': '1'},
                                   responses=[{'items': [{'id': 1, 'value': 'L'}], 'more': True,
                                               'total': 1}])])
    right = make_view('b_eval', 'frozen_evaluation',
                      endpoints=[ep(query={'page': '1'},
                                    responses=[{'rows': [{'id': 2, 'value': 'R'}], 'more': True,
                                                'total': 2, 'extra': 'x'}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.MANUAL_REVIEW
    assert result.strong_signals == ()
    assert result.weak_signals == (W4_PAGINATION_SEMANTICS_MATCH,)


def test_failure_code_only_is_manual_review():
    left = make_view('a_dev', 'development', expected_error_code='duplicate_id',
                     endpoints=[ep(query={'page': '1'}, responses=[{'a': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', expected_error_code='duplicate_id',
                      endpoints=[ep(query={}, responses=[{'b': 'x'}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.MANUAL_REVIEW
    assert result.strong_signals == ()
    assert result.weak_signals == (W5_FAILURE_CODE_MATCH,)


def test_repair_disposition_only_is_manual_review():
    left = make_view('a_dev', 'development', disposition='proposal_only',
                     endpoints=[ep(query={'page': '1'}, responses=[{'a': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', disposition='proposal_only',
                      endpoints=[ep(query={}, responses=[{'b': 'x'}])])
    result = compare(left, right)
    assert result.decision is OverlapDecision.MANUAL_REVIEW
    assert result.strong_signals == ()
    assert result.weak_signals == (W6_REPAIR_DISPOSITION_MATCH,)


def test_repair_disposition_none_is_not_a_signal():
    left = make_view('a_dev', 'development', disposition='none',
                     endpoints=[ep(query={'page': '1'}, responses=[{'a': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', disposition='none',
                      endpoints=[ep(query={}, responses=[{'b': 'x'}])])
    result = compare(left, right)
    assert W6_REPAIR_DISPOSITION_MATCH not in result.weak_signals


# --------------------------------------------------------------------------
# 13, 7(no escalation). multiple weak signals, no strong -> MANUAL_REVIEW
# --------------------------------------------------------------------------
def test_multiple_weak_signals_do_not_escalate_to_block():
    left = make_view('a_dev', 'development', expected_error_code='duplicate_id',
                     disposition='proposal_only',
                     endpoints=[ep(query={'page': '1'},
                                   responses=[{'items': [{'id': 1, 'value': 'L'}], 'more': True,
                                               'total': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', expected_error_code='duplicate_id',
                      disposition='proposal_only',
                      endpoints=[ep(query={'page': '1'},
                                    responses=[{'items': [{'id': 9, 'value': 'R'}], 'more': True,
                                                'total': 1}])])
    result = compare(left, right)
    assert result.strong_signals == ()
    assert set(result.weak_signals) == {W3_ENDPOINT_COMPOSITION_MATCH, W4_PAGINATION_SEMANTICS_MATCH,
                                        W5_FAILURE_CODE_MATCH, W6_REPAIR_DISPOSITION_MATCH}
    # Four weak signals and still only MANUAL_REVIEW: no weak-count escalation.
    assert result.decision is OverlapDecision.MANUAL_REVIEW


# --------------------------------------------------------------------------
# 15, 16. determinism: canonical pair, reversal, shuffle, repeat, hash order
# --------------------------------------------------------------------------
def test_compare_is_canonical_and_reversal_equal():
    a = blank_view('a_dev', 'development', records=[{'id': 1, 'value': 'x'}])
    b = blank_view('b_eval', 'frozen_evaluation', records=[{'id': 1, 'value': 'x'}])
    forward, backward = compare(a, b), compare(b, a)
    assert forward == backward
    assert forward.left_case_id == 'a_dev' and forward.right_case_id == 'b_eval'
    assert forward.decision is OverlapDecision.BLOCK  # identical record set -> S4
    assert compare(a, b) == forward  # repeat run is stable


def test_record_hash_order_does_not_change_the_result():
    left = make_view('a_dev', 'development', records=[{'id': 1, 'value': 'x'}, {'id': 2, 'value': 'y'}])
    right = make_view('b_eval', 'frozen_evaluation',
                      records=[{'id': 2, 'value': 'y'}, {'id': 1, 'value': 'x'}])
    result = compare(left, right)
    assert S4_EXACT_RECORD_SET in result.strong_signals
    assert result.decision is OverlapDecision.BLOCK


def test_audit_input_order_does_not_change_output():
    views = [make_view('c%d' % i, 'frozen_evaluation' if i % 2 == 0 else 'development',
                       family_id='f%d' % i, records=[{'id': i, 'value': 'v%d' % i}],
                       endpoints=[ep(responses=[{'items': [{'id': i, 'value': 'v%d' % i}],
                                                'total': i}])]) for i in range(6)]
    base = audit_cross_split(views)
    assert len(base) == 9  # 3 development x 3 frozen_evaluation
    rng = random.Random(1234)
    for _ in range(3):
        shuffled = list(views)
        rng.shuffle(shuffled)
        assert audit_cross_split(shuffled) == base
    assert audit_cross_split(list(reversed(views))) == base


# --------------------------------------------------------------------------
# 17. fail closed on duplicate / empty case id
# --------------------------------------------------------------------------
def test_duplicate_case_id_fails_closed():
    with pytest.raises(ValueError) as caught:
        audit_cross_split([make_view('dup', 'development'), make_view('dup', 'frozen_evaluation')])
    assert 'duplicate_case_id' in str(caught.value)


def test_empty_case_id_fails_closed():
    empty = NormalizedView(case_id='', split='development', family_id='f', family_signature_digest=SHA_A,
                           oracle_result_sha256=None, record_hashes=frozenset(),
                           response_payload_hashes=frozenset(), nested_subtree_hashes=frozenset(),
                           endpoint_composition_signature=SHA_A, pagination_signature=None,
                           failure_code=None, repair_disposition='none')
    with pytest.raises(ValueError) as caught:
        audit_cross_split([empty, make_view('x', 'frozen_evaluation')])
    assert 'empty_case_id' in str(caught.value)


def test_compare_rejects_same_case_id():
    view = make_view('solo', 'development')
    with pytest.raises(ValueError) as caught:
        compare(view, view)
    assert 'compare_requires_distinct_case_ids' in str(caught.value)


# --------------------------------------------------------------------------
# 18-20. empty / scalar containers are not signals
# --------------------------------------------------------------------------
def test_empty_record_set_is_not_an_exact_record_set():
    left = make_view('a_dev', 'development', records=None,
                     endpoints=[ep(query={'page': '1'}, responses=[{'x': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', records=None,
                      endpoints=[ep(query={}, responses=[{'y': 'z'}])])
    result = compare(left, right)
    assert S4_EXACT_RECORD_SET not in result.strong_signals
    assert W1_PARTIAL_RECORD_OVERLAP not in result.weak_signals


def test_empty_dict_and_list_are_not_subtree_signals():
    left = make_view('a_dev', 'development', endpoints=[ep(responses=[{'a': {}, 'x': 1}])])
    right = make_view('b_eval', 'frozen_evaluation', endpoints=[ep(responses=[{'a': [], 'y': 2}])])
    result = compare(left, right)
    assert W2_NESTED_RESPONSE_SUBTREE_OVERLAP not in result.weak_signals
    assert S5_EXACT_RESPONSE_PAYLOAD not in result.strong_signals


def test_equal_scalars_are_not_subtree_signals():
    left = make_view('a_dev', 'development', endpoints=[ep(responses=[{'a': 1, 'b': 'same'}])])
    right = make_view('b_eval', 'frozen_evaluation', endpoints=[ep(responses=[{'a': 2, 'b': 'same'}])])
    result = compare(left, right)
    assert W2_NESTED_RESPONSE_SUBTREE_OVERLAP not in result.weak_signals


# --------------------------------------------------------------------------
# 21. leakage safety
# --------------------------------------------------------------------------
def test_result_serialization_never_leaks_canary():
    canary_records = [{'id': 1, 'value': CANARY}]
    canary_payload = [{'items': [{'id': 1, 'value': CANARY}], 'note': CANARY, 'more': False}]
    left = make_view('a_dev', 'development', records=canary_records,
                     endpoints=[ep(responses=canary_payload)])
    right = make_view('b_eval', 'frozen_evaluation', records=canary_records,
                      endpoints=[ep(responses=canary_payload)])
    result = compare(left, right)
    assert result.decision is OverlapDecision.BLOCK
    blob = json.dumps(result.to_jsonable(), sort_keys=True)
    assert CANARY not in blob
    # The canary really was present in the source data the detector hashed.
    assert CANARY in json.dumps(canary_records) and CANARY in json.dumps(canary_payload)
    # Evidence is restricted to relations / counts / digests, never raw values.
    allowed = {'relation', 'count', 'digest', 'shared_count', 'left_count', 'right_count'}
    for fields in result.to_jsonable()['evidence'].values():
        assert set(fields) <= allowed


def test_normalized_view_holds_digests_not_raw_values():
    view = make_view('a_dev', 'development', records=[{'id': 1, 'value': CANARY}],
                     endpoints=[ep(responses=[{'items': [{'id': 1, 'value': CANARY}]}])])
    blob = json.dumps({'record_hashes': sorted(view.record_hashes),
                       'response_payload_hashes': sorted(view.response_payload_hashes),
                       'nested_subtree_hashes': sorted(view.nested_subtree_hashes),
                       'endpoint_composition_signature': view.endpoint_composition_signature,
                       'pagination_signature': view.pagination_signature})
    assert CANARY not in blob


# --------------------------------------------------------------------------
# 22. known A real regression (read from the frozen bundle, no copied fixture)
# --------------------------------------------------------------------------
def test_known_a_pair_blocks_by_real_evidence():
    assert_real_bundle_frozen()  # contract B: prove the bundle is frozen before projecting
    views = {v.case_id: v for v in load_m6_v1_views(ROOT)}
    result = compare(views['m6_get_flat'], views['m6_multi_endpoint'])
    assert result.decision is OverlapDecision.BLOCK
    assert S3_EXACT_ORACLE_RESULT_HASH in result.strong_signals
    assert S4_EXACT_RECORD_SET in result.strong_signals
    assert S5_EXACT_RESPONSE_PAYLOAD in result.strong_signals
    # The oracle hash in evidence is the real frozen value, not a hardcoded literal.
    assert (result.to_jsonable()['evidence'][S3_EXACT_ORACLE_RESULT_HASH]['digest']
            == views['m6_get_flat'].oracle_result_sha256)
    assert views['m6_get_flat'].oracle_result_sha256 == views['m6_multi_endpoint'].oracle_result_sha256


def test_detector_source_has_no_case_id_hardcode():
    source = OVERLAP_SRC.read_text(encoding='utf-8')
    for case_id in ('m6_get_flat', 'm6_multi_endpoint', 'm6_duplicate_id', 'm6_total_changed'):
        assert case_id not in source


def test_audit_cross_split_over_real_bundle():
    assert_real_bundle_frozen()  # contract B: prove the bundle is frozen before projecting
    views = load_m6_v1_views(ROOT)
    results = audit_cross_split(views)
    development = [v for v in views if v.split == 'development']
    frozen = [v for v in views if v.split == 'frozen_evaluation']
    assert len(results) == len(development) * len(frozen)
    keys = [(r.left_case_id, r.right_case_id) for r in results]
    assert keys == sorted(keys)  # canonically ordered
    by_id = {v.case_id: v for v in views}
    for r in results:
        assert r.left_case_id < r.right_case_id
        assert by_id[r.left_case_id].split != by_id[r.right_case_id].split  # cross-split only


# --------------------------------------------------------------------------
# Decision rule + static purity
# --------------------------------------------------------------------------
def test_decision_rule_is_fixed_three_state():
    assert OverlapDecision.AUTO_PASS.value == 'auto_pass'
    assert OverlapDecision.MANUAL_REVIEW.value == 'manual_review'
    assert OverlapDecision.BLOCK.value == 'block'
    assert {d.value for d in OverlapDecision} == {'auto_pass', 'manual_review', 'block'}


def test_overlap_module_imports_are_pure():
    tree = ast.parse(OVERLAP_SRC.read_text(encoding='utf-8'))
    roots, full = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split('.')[0])
                full.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split('.')[0])
            full.add(node.module)
    allowed_roots = {'json', 'collections', 'dataclasses', 'enum', 'pathlib', 'types',
                     '__future__', 'evaluation'}
    assert roots <= allowed_roots, roots - allowed_roots
    forbidden = ('backend.app.llm', 'gateway', 'requests', 'httpx', 'aiohttp', 'playwright',
                 'sqlite', 'repository', 'subprocess', 'socket', 'urllib')
    for module in full:
        for bad in forbidden:
            assert bad not in module, (module, bad)


def test_overlap_module_has_no_io_or_dynamic_exec_outside_loader():
    source = OVERLAP_SRC.read_text(encoding='utf-8')
    for token in ('eval(', 'exec(', 'subprocess', 'os.system', 'socket', 'urlopen',
                  '.write_text', '.write_bytes', 'open('):
        assert token not in source, token


def test_core_functions_are_io_free():
    core = {'normalize_case', 'compare', 'audit_cross_split', '_decide', '_container_kind',
            '_page_kind_and_location', '_continuation_flags', '_stop_signal',
            '_endpoint_composition', '_pagination_signature', '_collect_subtrees'}
    tree = ast.parse(OVERLAP_SRC.read_text(encoding='utf-8'))
    io_calls = {'read_text', 'write_text', 'read_bytes', 'write_bytes', 'open', 'Path'}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in core:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    name = func.attr if isinstance(func, ast.Attribute) else (
                        func.id if isinstance(func, ast.Name) else None)
                    assert name not in io_calls, (node.name, name)


def test_normalize_case_is_fail_closed():
    with pytest.raises(ValueError):
        normalize_case({}, {}, {}, {})
    with pytest.raises(ValueError):
        normalize_case({'id': 'x', 'split': 'development', 'family_id': 'f',
                        'repair_expectation': {'policy_disposition': 'none'}},
                       {'records': None}, {'endpoints': []}, {'sig': 1})
    with pytest.raises(ValueError):
        normalize_case({'id': '', 'split': 'development', 'family_id': 'f',
                        'repair_expectation': {'policy_disposition': 'none'}},
                       {'records': None}, {'endpoints': [{'method': 'GET', 'responses': [{}]}]}, {})
