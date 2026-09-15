"""M6.1-A assets and pure contracts; never a cloud evaluation runner."""
import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_representative_dataset_exists():
    path = ROOT / 'evaluation/datasets/m6-v1/manifest.json'
    assert path.is_file(), 'M6.1-A dataset has not been built'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    assert manifest['status'] == 'm61_a_review'
    assert len(manifest['cases']) == 12


def test_bundle_and_regeneration():
    from evaluation.schemas.contract import validate_bundle
    from evaluation.fixtures.build_dataset import build_assets
    first = build_assets()
    assert first == build_assets()
    for name, raw in first.items():
        assert (ROOT / name).read_text(encoding='utf-8').replace('\r\n', '\n') == raw
    cases = validate_bundle(ROOT)
    assert len(cases) == 12
    assert sum(c.split == 'development' for c in cases) == 5
    assert sum(c.split == 'frozen_evaluation' for c in cases) == 7


def test_canonical_contract():
    from evaluation.schemas.hashing import digest, canonical
    assert digest({'b': 2, 'a': '汉字'}) == digest({'a': '汉字', 'b': 2})
    assert digest([1, 2]) != digest([2, 1])
    assert canonical({'a': '汉字'}).decode('utf-8') == '{"a":"汉字"}'
    with pytest.raises(ValueError):
        canonical({'x': float('nan')})


def test_external_retrieval_labels_and_empty_denominator():
    from evaluation.schemas.scoring import score_retrieval
    assert score_retrieval(['x', 'y'], ['y']) == {'top1_hit': 0, 'top2_hit': 1, 'mrr_at_2': .5}
    assert score_retrieval(['x'], ['y'])['mrr_at_2'] == 0
    assert score_retrieval([], []) == {'top1_hit': None, 'top2_hit': None, 'mrr_at_2': None}
    with pytest.raises(ValueError):
        score_retrieval(['x', 'x'], ['x'])


def test_success_requires_independent_oracle_and_delivery():
    from evaluation.schemas.scoring import score_task_result
    from evaluation.schemas.hashing import digest
    records = [{'id': 1, 'value': 'a'}, {'id': 2, 'value': 'b'}]
    oracle = {'page_count': 2, 'record_count': 2, 'result_sha256': digest(records),
              'schema': {'id': 'integer', 'value': 'string'}, 'unique_key': 'id',
              'completeness': 'complete', 'records': records}
    report = {'status': 'succeeded', 'pages': 2, 'completeness': 'complete'}
    assert score_task_result(report, records, oracle)['task_success']
    assert not score_task_result(report, records, oracle)['delivery_success']
    for changed in ({**report, 'pages': 1}, {**report, 'status': 'failed'},
                    {**report, 'completeness': 'partial'}):
        assert not score_task_result(changed, records, oracle)['task_success']
    for wrong in ([records[0], records[0]], [{'id': True, 'value': 'a'}, records[1]],
                  [{'id': 1, 'value': 'wrong'}, records[1]]):
        assert not score_task_result(report, wrong, oracle)['task_success']
    flags = dict(collector_exported=True, collector_execution_success=True,
                 collector_matches_internal_result=True)
    assert not score_task_result({**report, **flags}, records, oracle)['delivery_success']
    exported = {'items': records, 'pages': 2, 'completeness': 'complete'}
    assert score_task_result({**report, **flags}, records, oracle, exported)['delivery_success']
    assert records == oracle['records']  # helpers did not mutate input


def test_expected_rejection_is_separate_and_fail_closed():
    from evaluation.schemas.scoring import score_expected_rejection
    expectation = {'allowed_codes': ['duplicate_id'], 'phase': 'executing', 'max_execution_requests': 2}
    assert score_expected_rejection('failed', 'duplicate_id', 'executing', 2, expectation)
    assert not score_expected_rejection('failed', 'total_changed', 'executing', 2, expectation)
    assert not score_expected_rejection('failed', 'duplicate_id', 'executing', None, expectation)
    assert not score_expected_rejection('failed', 'duplicate_id', 'executing', 3, expectation)


@pytest.mark.parametrize('field,value', [
    ('split', 'test'), ('categories', ['invented']), ('version', True),
    ('id', '../escape'), ('expected_outcome', 'maybe'), ('extra', 'forbidden'),
])
def test_schema_rejects_invalid_case(field, value):
    from evaluation.schemas.contract import Case, validate_bundle
    data = validate_bundle(ROOT)[0].model_dump()
    data[field] = value
    with pytest.raises(ValueError):
        Case.model_validate(data)


def test_taxonomy_and_repair_mutations_rejected():
    from evaluation.schemas.contract import Case, validate_case_semantics, validate_bundle
    data = validate_bundle(ROOT)[0].model_dump()
    data['failure_expectation']['allowed_codes'] = ['invented_error']
    with pytest.raises(ValueError):
        validate_case_semantics(Case.model_validate(data))
    data = validate_bundle(ROOT)[0].model_dump()
    data['repair_expectation']['may_apply'] = True
    with pytest.raises(ValueError):
        validate_case_semantics(Case.model_validate(data))


def test_family_leakage_and_renamed_templates_rejected():
    from evaluation.schemas.contract import validate_families
    families = [{'id': 'flat', 'split': 'development', 'signature': {'wire': 'flat'}},
                {'id': 'flat_renamed', 'split': 'frozen_evaluation', 'signature': {'wire': 'flat'}}]
    with pytest.raises(ValueError):
        validate_families(families)
    families[1]['id'] = 'flat'
    with pytest.raises(ValueError):
        validate_families(families)


def test_fixture_responses_and_oracle_isolation():
    from evaluation.schemas.contract import validate_bundle, read_json
    from evaluation.fixtures.runtime import planner_inputs, response_for
    for case in validate_bundle(ROOT):
        fixture = read_json(ROOT, case.fixture.ref)
        original = copy.deepcopy(fixture)
        public = planner_inputs(fixture)
        assert set(public) == {'fields', 'observations'}
        assert not any(word in json.dumps(public) for word in ('oracle', 'relevant_case_ids', 'result_sha256'))
        contaminated = {**fixture, 'oracle': {'private': 'ORACLE_SENTINEL'}}
        with pytest.raises(ValueError):
            planner_inputs(contaminated)
        for endpoint in fixture['endpoints']:
            for page, expected in enumerate(endpoint['responses'], 1):
                assert response_for(endpoint, page) == expected
                assert response_for(endpoint, page) == response_for(endpoint, page)
        assert fixture == original


def test_corpus_ablations_share_identity():
    from evaluation.schemas.contract import read_json
    groups = [read_json(ROOT, f'evaluation/corpora/m6-v1/{g}.json') for g in ('e1', 'e2', 'e3')]
    base = lambda group: [{k: c[k] for k in ('id', 'title', 'terms', 'guidance')} for c in group]
    assert base(groups[0]) == base(groups[1]) == base(groups[2])
    assert all('features' not in c for c in groups[0])
    assert all('failure_modes' not in c for c in groups[1])


def test_static_runtime_boundary_and_canary():
    import ast
    from evaluation.schemas.contract import read_json
    from evaluation.fixtures.runtime import planner_inputs
    source = (ROOT / 'evaluation/fixtures/runtime.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id in {'open', 'eval', 'exec', '__import__'} for n in ast.walk(tree))
    assert 'scoring' not in source and 'read_text' not in source
    paths = list((ROOT / 'evaluation').rglob('*.json'))
    hits = [p for p in paths if 'M6_CANARY_SECRET_001' in p.read_text(encoding='utf-8')]
    assert len(hits) == 1 and hits[0].parent.name == 'inputs'
    public = planner_inputs(read_json(ROOT, hits[0].relative_to(ROOT).as_posix()))
    assert 'M6_CANARY_SECRET_001' not in json.dumps(public)
