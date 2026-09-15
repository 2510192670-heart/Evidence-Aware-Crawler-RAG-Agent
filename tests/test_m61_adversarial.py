"""Deliberate invalid assets must be rejected even after recomputing hashes."""
import copy
import json
from pathlib import Path
import shutil
import re

import pytest

from evaluation.schemas.contract import validate_bundle, read_json, Case, validate_case_semantics
from evaluation.schemas.hashing import digest

ROOT = Path(__file__).resolve().parents[1]
BASE = 'evaluation/datasets/m6-v1/'


def write(root, ref, data):
    (root / ref).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def bundle(tmp_path):
    shutil.copytree(ROOT / 'evaluation', tmp_path / 'evaluation')
    return tmp_path


def reseal(root):
    manifest = read_json(root, BASE + 'manifest.json')
    for ref in manifest['cases']:
        case = read_json(root, ref)
        for key in ('fixture', 'oracle', 'retrieval_labels'):
            case['hashes'][key] = digest(read_json(root, case[key]['ref']))
        write(root, ref, case)
    for ref in manifest['files']:
        manifest['files'][ref] = digest(read_json(root, ref))
    write(root, BASE + 'manifest.json', manifest)
    (root / (BASE + 'manifest.sha256')).write_text(digest(manifest) + '\n', encoding='utf-8')


@pytest.mark.parametrize('target', ['manifest', 'case', 'fixture', 'oracle', 'corpus', 'source'])
def test_hash_tampering_detected(bundle, target):
    paths = {
        'manifest': BASE + 'manifest.json', 'case': BASE + 'cases/m6_get_flat.json',
        'fixture': BASE + 'inputs/m6_get_flat.json', 'oracle': BASE + 'oracles/m6_get_flat.json',
        'corpus': 'evaluation/corpora/m6-v1/e1.json', 'source': 'evaluation/fixtures/runtime.py'}
    path = bundle / paths[target]
    if target == 'source':
        path.write_text(path.read_text(encoding='utf-8') + '\n# tampered\n', encoding='utf-8')
    else:
        value = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(value, dict):
            value['tampered'] = True
        else:
            value[0]['title'] = 'changed'
        write(bundle, paths[target], value)
    with pytest.raises(ValueError):
        validate_bundle(bundle)


@pytest.mark.parametrize('mutation', ['unknown_label', 'bad_count', 'bad_schema', 'duplicate_id',
                                      'split', 'unsafe_repair', 'oracle_in_response', 'identity',
                                      'features', 'missing_reference'])
def test_semantic_tampering_detected_after_rehash(bundle, mutation):
    case_ref = BASE + 'cases/m6_get_flat.json'
    case = read_json(bundle, case_ref)
    if mutation == 'unknown_label':
        value = read_json(bundle, case['retrieval_labels']['ref'])
        value['relevant_case_ids'] = ['does_not_exist']
        write(bundle, case['retrieval_labels']['ref'], value)
    elif mutation in ('bad_count', 'bad_schema'):
        value = read_json(bundle, case['oracle']['ref'])
        value['record_count' if mutation == 'bad_count' else 'schema'] = 99 if mutation == 'bad_count' else {'id': 'string'}
        write(bundle, case['oracle']['ref'], value)
    elif mutation == 'oracle_in_response':
        value = read_json(bundle, case['fixture']['ref'])
        value['endpoints'][0]['responses'][0]['oracle'] = {'expected_outcome': 'success'}
        write(bundle, case['fixture']['ref'], value)
    elif mutation in ('identity', 'features'):
        ref = 'evaluation/corpora/m6-v1/e3.json'
        value = read_json(bundle, ref)
        if mutation == 'identity':
            value[0]['terms'].append('answer')
        else:
            value[0]['features']['method'] = 'POST'
        write(bundle, ref, value)
    else:
        if mutation == 'duplicate_id':
            case['id'] = 'm6_duplicate_id'
        elif mutation == 'split':
            case['split'] = 'frozen_evaluation'
        elif mutation == 'unsafe_repair':
            case['repair_expectation']['may_apply'] = True
        else:
            case['fixture']['ref'] = 'evaluation/absent.json'
        write(bundle, case_ref, case)
    with pytest.raises(ValueError):
        reseal(bundle)
        validate_bundle(bundle)


@pytest.mark.parametrize('ref', ['../data/tasks/task/report.json', 'E:/private/oracle.json',
                                'evaluation/../data/x.json', 'evaluation\\x.json'])
def test_path_escape_rejected(ref):
    with pytest.raises(ValueError):
        read_json(ROOT, ref)


def test_json_duplicate_key_and_nan_rejected(bundle):
    path = BASE + 'inputs/m6_get_flat.json'
    for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
        (bundle / path).write_text(raw, encoding='utf-8')
        with pytest.raises(ValueError):
            read_json(bundle, path)


def test_unindexed_case_is_not_silently_ignored(bundle):
    source = bundle / (BASE + 'cases/m6_get_flat.json')
    shutil.copyfile(source, source.with_name('unindexed.json'))
    with pytest.raises(ValueError):
        validate_bundle(bundle)


def test_policy_is_read_from_authoritative_definitions():
    from backend.app.pipeline.repair import APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS
    from backend.app.rag.knowledge import project_repairability
    assert APPLICABLE_ERROR_CODES == frozenset({'pointer_not_found'})
    assert MAX_REPAIR_ATTEMPTS == 1
    assert project_repairability('page_location_mismatch') == 'proposal_only'
    assert project_repairability('duplicate_id') == 'rejected'


def test_scoring_rejects_missing_observations_and_uses_external_ids():
    from evaluation.schemas.scoring import score_task_result, score_retrieval
    oracle = read_json(ROOT, BASE + 'oracles/m6_get_flat.json')
    assert not score_task_result({'status': 'succeeded'}, oracle['records'], oracle)['task_success']
    # Scores depend only on external labels, never on returned failure_modes metadata.
    a = [{'id': 'flat_get'}]
    b = [{'id': 'flat_get', 'failure_modes': []}]
    assert score_retrieval([r['id'] for r in a], ['flat_get']) == score_retrieval([r['id'] for r in b], ['flat_get'])


def test_formatting_and_crlf_do_not_change_logical_hashes(bundle):
    manifest = read_json(bundle, BASE + 'manifest.json')
    for ref in [*manifest['files'], BASE + 'manifest.json']:
        obj = read_json(bundle, ref)
        (bundle / ref).write_bytes(json.dumps(obj, ensure_ascii=False, indent=4).replace('\n', '\r\n').encode('utf-8'))
    for ref in manifest['sources']:
        path = bundle / ref
        path.write_bytes(path.read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8'))
    assert len(validate_bundle(bundle)) == 12


def test_frozen_json_has_no_accidental_credentials_or_local_paths():
    from backend.app.pipeline.contracts import SENSITIVE
    allowed = BASE + 'inputs/m6_get_flat.json'
    secret = 'M6_CANARY_SECRET_001'
    suspicious = re.compile(r'-----BEGIN .*PRIVATE KEY|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{16,}|'
                            r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|(?<![A-Za-z])[A-Za-z]:[\\/]')
    def walk(value, ref):
        if isinstance(value, dict):
            for key, child in value.items():
                if SENSITIVE.search(key):
                    assert ref == allowed and key == 'secret' and child == secret, ref
                walk(child, ref)
        elif isinstance(value, list):
            for child in value:
                walk(child, ref)
        elif isinstance(value, str):
            assert not suspicious.search(value), ref
            if secret in value:
                assert ref == allowed, ref
    for path in (ROOT / 'evaluation').rglob('*.json'):
        walk(json.loads(path.read_text(encoding='utf-8')), path.relative_to(ROOT).as_posix())
