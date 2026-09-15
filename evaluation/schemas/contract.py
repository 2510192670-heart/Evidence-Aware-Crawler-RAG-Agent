"""M6.1-A evaluation-only schema and read-only bundle integrity validation."""
import json
from pathlib import Path, PurePosixPath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from backend.app.pipeline.errors import SPECS
from backend.app.pipeline.contracts import SENSITIVE
from backend.app.pipeline.repair import APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS
from backend.app.rag.knowledge import load as load_knowledge, project_repairability, SCENARIO_VOCABULARY
from evaluation.fixtures.runtime import validate_input
from .hashing import digest, source_digest

ID = r'^[a-z][a-z0-9_]{1,63}$'
SHA = r'^[a-f0-9]{64}$'
Category = Literal['collection', 'get', 'post', 'nested', 'alias', 'multiple_candidates',
                   'data_integrity', 'duplicate_id', 'total_changed', 'field_type_changed',
                   'empty_page_with_next', 'cursor', 'offset', 'unsupported', 'repair',
                   'pointer_not_found', 'applied', 'ambiguous', 'diagnostic', 'canary',
                   'proposal_only', 'budget_exhausted', 'unclassified']


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Ref(Closed):
    ref: str

    @field_validator('ref')
    @classmethod
    def relative_path(cls, value):
        path = PurePosixPath(value)
        if (not value.startswith('evaluation/') or '\\' in value or ':' in value
                or '..' in path.parts or path.is_absolute() or str(path) != value):
            raise ValueError('invalid_relative_reference')
        return value


class Fixture(Ref):
    fixture_id: str = Field(pattern=ID)
    generator_version: Literal['m61-a-1']


class Failure(Closed):
    classified: bool
    allowed_codes: list[str]
    phase: Literal['observing', 'analyzing', 'executing', 'unknown'] | None
    max_execution_requests: StrictInt = Field(ge=0, le=20)
    exception_type: Literal['ValueError'] | None


class Repair(Closed):
    trigger_code: str | None
    policy_disposition: Literal['none', 'auto_applied', 'proposal_only', 'rejected']
    eligible: bool
    may_apply: bool
    expected_applied: bool
    max_attempts: StrictInt = Field(ge=0, le=1)


class Provenance(Closed):
    source: Literal['m6_authored']
    natural_failure: Literal[False]
    injected_failure: bool
    injection: Literal['none', 'response_drift', 'missing_items_pointer', 'unsupported_page_plan']
    note: str


class Request(Ref):
    method: Literal['GET', 'POST']


class Hashes(Closed):
    fixture: str = Field(pattern=SHA)
    oracle: str = Field(pattern=SHA)
    retrieval_labels: str = Field(pattern=SHA)


class Case(Closed):
    id: str = Field(pattern=ID)
    family_id: str = Field(pattern=ID)
    version: StrictInt = Field(ge=1, le=1)
    split: Literal['development', 'frozen_evaluation']
    categories: list[Category] = Field(min_length=1)
    evidence_level: Literal['component_fixture']
    supported: bool
    expected_outcome: Literal['success', 'expected_rejection', 'controlled_failure']
    fixture: Fixture
    request: Request
    oracle: Ref
    retrieval_labels: Ref
    failure_expectation: Failure
    repair_expectation: Repair
    provenance: Provenance
    hashes: Hashes


class Oracle(Closed):
    records: list[dict] | None
    page_count: StrictInt | None = Field(ge=1, le=10)
    record_count: StrictInt | None = Field(ge=0)
    result_sha256: str | None = Field(pattern=SHA)
    output_schema: dict[str, Literal['integer', 'number', 'string', 'boolean']] = Field(alias='schema')
    unique_key: str | None
    completeness: Literal['complete'] | None
    expected_error_code: str | None
    expected_error_phase: Literal['observing', 'analyzing', 'executing', 'unknown'] | None
    expected_exception_type: Literal['ValueError'] | None


class Labels(Closed):
    relevant_case_ids: list[str]
    failure_relevant_case_ids: list[str]
    rationale: str


def read_json(root, ref):
    Ref(ref=ref)
    root = Path(root).resolve()
    path = (root / ref).resolve()
    if not path.is_relative_to(root / 'evaluation') or not path.is_file():
        raise ValueError('missing_or_escaping_reference')
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('nonfinite_json')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                      parse_constant=invalid_constant)


def validate_case_semantics(case):
    failure, repair = case.failure_expectation, case.repair_expectation
    if len(set(case.categories)) != len(case.categories):
        raise ValueError('duplicate_category')
    codes = failure.allowed_codes
    if len(set(codes)) != len(codes) or any(code not in SPECS for code in codes):
        raise ValueError('invalid_error_code')
    if failure.classified != bool(codes) or any(SPECS[c].phase != failure.phase for c in codes):
        raise ValueError('failure_classification_mismatch')
    if (case.expected_outcome == 'success') != (not codes and failure.phase is None):
        raise ValueError('outcome_mismatch')
    if failure.classified and failure.exception_type is not None:
        raise ValueError('classified_exception_conflict')
    if not failure.classified and case.expected_outcome != 'success' and (
            failure.phase != 'unknown' or failure.exception_type is None):
        raise ValueError('invalid_unclassified_expectation')
    if not case.supported and case.expected_outcome != 'expected_rejection':
        raise ValueError('unsupported_must_reject')
    if case.provenance.injected_failure != (case.provenance.injection != 'none'):
        raise ValueError('injection_provenance_mismatch')
    if repair.trigger_code is None:
        if (repair.policy_disposition != 'none' or repair.eligible or repair.may_apply
                or repair.expected_applied or repair.max_attempts):
            raise ValueError('repair_without_trigger')
    else:
        disposition = project_repairability(repair.trigger_code)
        if repair.policy_disposition != disposition:
            raise ValueError('repair_policy_mismatch')
        if repair.eligible != (disposition != 'rejected'):
            raise ValueError('repair_eligibility_mismatch')
        if repair.may_apply != (repair.trigger_code in APPLICABLE_ERROR_CODES):
            raise ValueError('repair_apply_mismatch')
        if repair.expected_applied and not repair.may_apply:
            raise ValueError('forbidden_expected_application')
        if repair.max_attempts != (MAX_REPAIR_ATTEMPTS if repair.eligible else 0):
            raise ValueError('repair_budget_mismatch')
    if repair.trigger_code is not None and case.provenance.injection == 'none':
        raise ValueError('missing_failure_provenance')


def _shape(value):
    # Ignore field names, values, IDs and repeated same-shaped members.
    if isinstance(value, dict):
        return ['object', sorted({json.dumps(_shape(v), sort_keys=True) for k, v in value.items()
                                  if not SENSITIVE.search(k)})]
    if isinstance(value, list):
        return ['array', sorted({json.dumps(_shape(v), sort_keys=True) for v in value})]
    return type(value).__name__


def family_signature(fixture):
    endpoints = []
    for e in fixture['endpoints']:
        keys = set(e['query']) | set(e['request_body'] or {})
        paging = 'cursor' if 'cursor' in keys else 'offset' if 'offset' in keys else 'page_number'
        endpoints.append({'method': e['method'], 'paging': paging,
                          'query_present': bool(e['query']),
                          'body_shape': _shape(e['responses'][0])})
    return {'endpoints': sorted(endpoints, key=lambda x: json.dumps(x, sort_keys=True))}


def validate_families(families):
    ids, signature_splits = set(), {}
    for family in families:
        if (set(family) != {'id', 'split', 'signature'} or not re.fullmatch(ID, family['id'])
                or family['id'] in ids or family['split'] not in {'development', 'frozen_evaluation'}):
            raise ValueError('invalid_family')
        ids.add(family['id'])
        signature = digest(family['signature'])
        if signature in signature_splits and signature_splits[signature] != family['split']:
            raise ValueError('renamed_template_cross_split')
        signature_splits[signature] = family['split']


def validate_bundle(root):
    root = Path(root)
    base = 'evaluation/datasets/m6-v1/'
    manifest = read_json(root, base + 'manifest.json')
    if set(manifest) != {'version', 'status', 'baseline', 'cases', 'families', 'files', 'sources'}:
        raise ValueError('invalid_manifest')
    if (type(manifest['version']) is not int or manifest['version'] != 1 or manifest['status'] != 'm61_a_review'
            or manifest['baseline'] != '15c5678a963665412f5e68e29617ddd7469b844d'
            or not isinstance(manifest['cases'], list) or len(set(manifest['cases'])) != len(manifest['cases'])
            or not isinstance(manifest['files'], dict) or not isinstance(manifest['sources'], dict)):
        raise ValueError('invalid_manifest_version')
    if digest(manifest) != (root / (base + 'manifest.sha256')).read_text(encoding='utf-8').strip():
        raise ValueError('manifest_hash_mismatch')
    for ref, expected in manifest['files'].items():
        if digest(read_json(root, ref)) != expected:
            raise ValueError('file_hash_mismatch')
    for ref, expected in manifest['sources'].items():
        Ref(ref=ref)
        path = (root / ref).resolve()
        if not path.is_relative_to(root.resolve() / 'evaluation') or source_digest(path.read_text(encoding='utf-8')) != expected:
            raise ValueError('source_hash_mismatch')
    if set(manifest['sources']) != {'evaluation/fixtures/build_dataset.py', 'evaluation/fixtures/runtime.py',
                                    'evaluation/schemas/contract.py', 'evaluation/schemas/hashing.py',
                                    'evaluation/schemas/scoring.py'}:
        raise ValueError('incomplete_source_inventory')
    families = read_json(root, manifest['families'])
    validate_families(families)
    by_family = {f['id']: f for f in families}
    corpora = [read_json(root, f'evaluation/corpora/m6-v1/{g}.json') for g in ('e1', 'e2', 'e3')]
    for corpus in corpora:
        load_knowledge(corpus)
    base_keys = {'id', 'title', 'terms', 'guidance'}
    for group, extra in zip(corpora, (set(), {'features'}, {'features', 'failure_modes', 'repairability'})):
        for row in group:
            if set(row) != base_keys | extra:
                raise ValueError('invalid_ablation_fields')
            for key, value in row.get('features', {}).items():
                if key not in SCENARIO_VOCABULARY or value not in SCENARIO_VOCABULARY[key]:
                    raise ValueError('invalid_feature_metadata')
    identity = lambda group: [{k: c[k] for k in ('id', 'title', 'terms', 'guidance')} for c in group]
    if not identity(corpora[0]) == identity(corpora[1]) == identity(corpora[2]):
        raise ValueError('ablation_identity_changed')
    if [c['features'] for c in corpora[1]] != [c['features'] for c in corpora[2]]:
        raise ValueError('ablation_features_changed')
    if read_json(root, 'evaluation/schemas/case.schema.json') != Case.model_json_schema():
        raise ValueError('published_schema_mismatch')
    corpus_ids = {c['id'] for c in corpora[0]}
    result, ids = [], set()
    for ref in manifest['cases']:
        case = Case.model_validate(read_json(root, ref))
        validate_case_semantics(case)
        if case.id in ids or case.family_id not in by_family:
            raise ValueError('duplicate_case_or_unknown_family')
        ids.add(case.id)
        fixture = read_json(root, case.fixture.ref)
        validate_input(fixture)
        family = by_family[case.family_id]
        if case.split != family['split'] or family_signature(fixture) != family['signature']:
            raise ValueError('family_signature_mismatch')
        if (case.fixture.fixture_id != case.id or case.request.ref != case.fixture.ref
                or case.request.method != fixture['endpoints'][0]['method']):
            raise ValueError('request_mismatch')
        oracle_raw = read_json(root, case.oracle.ref)
        oracle = Oracle.model_validate(oracle_raw)
        labels = Labels.model_validate(read_json(root, case.retrieval_labels.ref))
        for relevant in (labels.relevant_case_ids, labels.failure_relevant_case_ids):
            if len(set(relevant)) != len(relevant) or not set(relevant) <= corpus_ids:
                raise ValueError('invalid_retrieval_labels')
        for name in ('fixture', 'oracle', 'retrieval_labels'):
            if digest(read_json(root, getattr(case, name).ref)) != getattr(case.hashes, name):
                raise ValueError('case_dependency_hash_mismatch')
        if case.expected_outcome == 'success':
            from .scoring import score_task_result
            if (oracle.records is None or oracle.expected_error_code is not None
                    or oracle.expected_error_phase is not None or oracle.expected_exception_type is not None or not oracle.output_schema
                    or oracle.unique_key not in oracle.output_schema
                    or not score_task_result({'status': 'succeeded', 'pages': oracle.page_count,
                                              'completeness': oracle.completeness}, oracle.records, oracle_raw)['task_success']):
                raise ValueError('invalid_success_oracle')
        elif (oracle.records is not None or oracle.page_count is not None or oracle.record_count is not None
              or oracle.result_sha256 is not None or oracle.output_schema or oracle.unique_key is not None
              or oracle.completeness is not None
              or (oracle.expected_error_code not in case.failure_expectation.allowed_codes
                  if case.failure_expectation.classified else oracle.expected_error_code is not None)
              or oracle.expected_error_phase != case.failure_expectation.phase
              or oracle.expected_exception_type != case.failure_expectation.exception_type):
            raise ValueError('invalid_failure_oracle')
        result.append(case)
    referenced = set(manifest['cases']) | {manifest['families']} | {
        f'evaluation/corpora/m6-v1/{g}.json' for g in ('e1', 'e2', 'e3')} | {'evaluation/schemas/case.schema.json'}
    for case in result:
        referenced.update(getattr(case, key).ref for key in ('fixture', 'oracle', 'retrieval_labels'))
    if referenced != set(manifest['files']):
        raise ValueError('incomplete_hash_inventory')
    actual_json = {p.relative_to(root).as_posix() for folder in
                   (root / base, root / 'evaluation/corpora/m6-v1', root / 'evaluation/schemas')
                   for p in folder.rglob('*.json')}
    if actual_json != referenced | {base + 'manifest.json'}:
        raise ValueError('unindexed_json_asset')
    return result
