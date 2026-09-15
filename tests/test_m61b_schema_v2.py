"""M6.1-B1 Case Schema V2, version dispatch and the M6.1-A freeze guard.

Offline and model-free. Repair expectations are checked against the real
production proposer, policy checker and taxonomy rather than a copied vocabulary,
so a V2 expectation that validates is one the frozen code can actually produce.
"""
import json
import shutil
from pathlib import Path

import pytest

from backend.app.pipeline.contracts import (ExtractionPlan, Observation, plan_hash, validate_plan)
from backend.app.pipeline.errors import SPECS, Category, PipelineError, classify
from backend.app.pipeline.repair import (APPLICABLE_ERROR_CODES, DEFAULT_GENERATORS,
                                         MAX_REPAIR_ATTEMPTS, ProposalOutcome, ProposalReason,
                                         RepairContext, RepairOutcome, RepairReason,
                                         evaluate_failure, is_applicable)
from backend.app.rag.knowledge import DISPOSITIONS, project_repairability
from evaluation.schemas.contract import Case, validate_bundle, validate_case_semantics
from evaluation.schemas.contract_v2 import (CASE_V2_SCHEMA_REF, CaseV2, ExposureEvent,
                                            OracleV2, assert_m61a_frozen, dispatch_case,
                                            validate_case_semantics_v2, validate_oracle_v2)
from evaluation.schemas.hashing import digest, source_digest
from evaluation.schemas.scoring import score_task_result

ROOT = Path(__file__).resolve().parents[1]
INPUT_REF = 'evaluation/datasets/m6-v1b/inputs/m6b_probe.json'
ORACLE_REF = 'evaluation/datasets/m6-v1b/oracles/m6b_probe.json'
LABELS_REF = 'evaluation/datasets/m6-v1b/labels/m6b_probe.json'
HASH = '0' * 64
RECORDS = [{'id': 1, 'value': 'alpha'}, {'id': 2, 'value': 'beta'}]


# --------------------------------------------------------------------------
# Builders for syntactically valid V2 documents. Nothing here reads a dataset.
# --------------------------------------------------------------------------
def no_failure():
    return {'occurs': False, 'classified': False, 'allowed_codes': [], 'category': None,
            'phase': None, 'exception_type': None, 'max_execution_requests': 0}


def classified_failure(code, max_requests=2):
    spec = SPECS[code]
    return {'occurs': True, 'classified': True, 'allowed_codes': [code],
            'category': spec.category.value, 'phase': spec.phase, 'exception_type': None,
            'max_execution_requests': max_requests}


def unclassified_failure(max_requests=0):
    return {'occurs': True, 'classified': False, 'allowed_codes': [],
            'category': Category.UNCLASSIFIED.value, 'phase': 'unknown',
            'exception_type': 'ValueError', 'max_execution_requests': max_requests}


def no_repair():
    return {'trigger_code': None, 'policy_disposition': 'none', 'eligible': False,
            'expected_rejection_reason': None, 'expected_proposal_outcome': None,
            'expected_proposal_reason': None, 'expected_attempt_outcome': None,
            'may_apply': False, 'expected_applied': False, 'max_attempts': 0}


def unclassified_repair():
    return {**no_repair(), 'expected_rejection_reason': RepairReason.UNCLASSIFIED.value,
            'expected_attempt_outcome': RepairOutcome.REJECTED.value}


def refused_repair(trigger, reason):
    # may_apply is policy permission and stays true for an allowlisted code even
    # when this particular attempt is refused; only expected_applied requires both.
    return {**no_repair(), 'trigger_code': trigger,
            'policy_disposition': project_repairability(trigger),
            'may_apply': trigger in APPLICABLE_ERROR_CODES,
            'expected_rejection_reason': reason,
            'expected_attempt_outcome': RepairOutcome.REJECTED.value}


def eligible_repair(trigger, outcome, reason, applied=False):
    return {**no_repair(), 'trigger_code': trigger,
            'policy_disposition': project_repairability(trigger), 'eligible': True,
            'expected_proposal_outcome': outcome, 'expected_proposal_reason': reason,
            'expected_attempt_outcome': RepairOutcome.DEFERRED.value,
            'max_attempts': MAX_REPAIR_ATTEMPTS,
            'may_apply': trigger in APPLICABLE_ERROR_CODES, 'expected_applied': applied}


def provenance(injection='none'):
    return {'source': 'm6_authored', 'natural_failure': False,
            'injected_failure': injection != 'none', 'injection': injection,
            'note': 'B1 synthetic schema probe; no dataset asset exists.'}


def v2_case(**over):
    case = {
        'id': 'm6b_probe', 'family_id': 'probe_family', 'version': 2, 'split': 'development',
        'categories': ['collection', 'get'], 'evidence_level': 'component_fixture',
        'supported': True, 'expected_outcome': 'success', 'exposure': 'author_exposed',
        'exposure_history': [{'role': 'dataset_author', 'phase': 'm61_b1', 'event': 'authored'}],
        'fixture': {'ref': INPUT_REF, 'fixture_id': 'm6b_probe',
                    'generator_version': 'm61-b1-1'},
        'request': {'ref': INPUT_REF, 'method': 'GET'},
        'oracle': {'ref': ORACLE_REF}, 'retrieval_labels': {'ref': LABELS_REF},
        'failure_expectation': no_failure(), 'repair_expectation': no_repair(),
        'provenance': provenance(),
        'hashes': {'fixture': HASH, 'oracle': HASH, 'retrieval_labels': HASH},
    }
    case.update(over)
    return case


def output_oracle(records, pages, completeness):
    return {'records': records, 'page_count': pages, 'record_count': len(records),
            'result_sha256': digest(records), 'schema': {'id': 'integer', 'value': 'string'},
            'unique_key': 'id', 'completeness': completeness, 'expected_error_code': None,
            'expected_error_phase': None, 'expected_exception_type': None}


def failure_oracle(code=None, phase='unknown', exception_type='ValueError'):
    return {'records': None, 'page_count': None, 'record_count': None, 'result_sha256': None,
            'schema': {}, 'unique_key': None, 'completeness': None,
            'expected_error_code': code, 'expected_error_phase': phase,
            'expected_exception_type': exception_type}


def check(case_data, oracle_raw):
    """Run the full V2 contract over one case and its oracle."""
    case = CaseV2.model_validate(case_data)
    validate_case_semantics_v2(case)
    validate_oracle_v2(case, OracleV2.model_validate(oracle_raw), oracle_raw)
    return case


def probe_observation(method='GET', query=None, body=None, payload=None):
    return Observation(request_id='req_probe', method=method, url='http://127.0.0.1:18199/probe',
                       query={'page': '1'} if query is None else query, request_body=body,
                       body={'records': [{'id': 1, 'value': 'alpha'}], 'total': 1}
                       if payload is None else payload)


def probe_plan(**over):
    kwargs = dict(request_id='req_probe', items_pointer='/records',
                  fields={'id': '/id', 'value': '/value'}, unique_key='id',
                  page_parameter='page', total_pointer='/total', has_next_pointer=None)
    kwargs.update(over)
    return ExtractionPlan(**kwargs)


def audit(error, plan, record, attempts=0):
    return evaluate_failure(error, RepairContext(original_plan_hash=plan_hash(plan),
                                                 attempts=attempts), plan, record)


# --------------------------------------------------------------------------
# A. V1 keeps its own contract
# --------------------------------------------------------------------------
def test_v1_cases_still_use_the_v1_contract():
    cases = validate_bundle(ROOT)
    assert len(cases) == 12
    for case in cases:
        assert type(case) is Case
        raw = case.model_dump()
        assert raw['version'] == 1
        routed = dispatch_case(raw, 1)
        assert type(routed) is Case
        validate_case_semantics(routed)


def test_v1_case_is_rejected_by_the_v2_model():
    raw = validate_bundle(ROOT)[0].model_dump()
    with pytest.raises(ValueError):
        CaseV2.model_validate(raw)


# --------------------------------------------------------------------------
# B-G. Legitimate V2 shapes
# --------------------------------------------------------------------------
def test_valid_v2_success_complete():
    case = check(v2_case(), output_oracle(RECORDS, 2, 'complete'))
    assert case.expected_outcome == 'success'
    assert case.failure_expectation.occurs is False
    assert case.repair_expectation.trigger_code is None


def test_valid_v2_success_stop_condition_only():
    case = check(v2_case(), output_oracle(RECORDS, 2, 'stop_condition_only'))
    assert case.expected_outcome == 'success'


def test_valid_v2_partial_boundary():
    partial = RECORDS[:1]
    case = check(v2_case(expected_outcome='partial_boundary'), output_oracle(partial, 1, 'partial'))
    assert case.failure_expectation.occurs is False
    assert case.repair_expectation.trigger_code is None


def test_partial_oracle_satisfies_the_success_scorer_so_metrics_must_dispatch():
    """Records a trap for B6 instead of pretending the scorer isolates a partial.

    ``score_task_result`` only checks report/oracle agreement, so a partial oracle
    does score as a task success when the report also says ``partial``. Task Success
    Rate and Partial Boundary Accuracy must therefore be separated by dispatching on
    ``expected_outcome``; a partial_boundary case must never enter a success
    denominator. The contract-level guard is the outcome/completeness pairing.
    """
    partial = RECORDS[:1]
    oracle_raw = output_oracle(partial, 1, 'partial')
    assert score_task_result({'status': 'succeeded', 'pages': 1,
                              'completeness': 'partial'}, partial, oracle_raw)['task_success']
    with pytest.raises(ValueError) as caught:
        validate_oracle_v2(CaseV2.model_validate(v2_case()),
                           OracleV2.model_validate(oracle_raw), oracle_raw)
    assert 'success_requires_terminal_completeness' in str(caught.value)


def test_valid_v2_classified_expected_rejection():
    code = 'duplicate_id'
    case = check(v2_case(expected_outcome='expected_rejection',
                         failure_expectation=classified_failure(code),
                         repair_expectation=refused_repair(code, RepairReason.NOT_REPAIRABLE.value),
                         provenance=provenance('response_drift')),
                 failure_oracle(code, SPECS[code].phase, None))
    assert case.failure_expectation.category is SPECS[code].category
    assert case.repair_expectation.expected_proposal_outcome is None


def test_valid_v2_unclassified_expected_rejection():
    case = check(v2_case(expected_outcome='expected_rejection', supported=False,
                         failure_expectation=unclassified_failure(),
                         repair_expectation=unclassified_repair(),
                         provenance=provenance('unsupported_page_plan')),
                 failure_oracle(None, 'unknown', 'ValueError'))
    assert case.failure_expectation.category is Category.UNCLASSIFIED
    assert case.repair_expectation.expected_rejection_reason is RepairReason.UNCLASSIFIED


def test_valid_v2_controlled_failure():
    case = check(v2_case(expected_outcome='controlled_failure',
                         failure_expectation=classified_failure('pointer_not_found', 0),
                         repair_expectation=eligible_repair(
                             'pointer_not_found', ProposalOutcome.REJECTED.value,
                             ProposalReason.NO_DETERMINISTIC_CANDIDATE.value),
                         provenance=provenance('missing_items_pointer')),
                 failure_oracle('pointer_not_found', 'analyzing', None))
    assert case.repair_expectation.may_apply is True
    assert case.repair_expectation.expected_applied is False


# --------------------------------------------------------------------------
# H-I. Version dispatch fails closed
# --------------------------------------------------------------------------
@pytest.mark.parametrize('version', [3, 0, -1, '2', True, None, 2.0])
def test_unknown_case_version_rejected(version):
    with pytest.raises(ValueError) as caught:
        dispatch_case({**v2_case(), 'version': version}, 2)
    assert 'unknown_case_version' in str(caught.value)


@pytest.mark.parametrize('declared', [3, 0, '2', None, True])
def test_unknown_manifest_schema_version_rejected(declared):
    with pytest.raises(ValueError) as caught:
        dispatch_case(v2_case(), declared)
    assert 'unknown_case_schema_version' in str(caught.value)


def test_mixed_case_schema_version_rejected():
    v1_raw = validate_bundle(ROOT)[0].model_dump()
    with pytest.raises(ValueError) as caught:
        dispatch_case(v1_raw, 2)
    assert 'mixed_case_schema_version' in str(caught.value)
    with pytest.raises(ValueError) as caught:
        dispatch_case(v2_case(), 1)
    assert 'mixed_case_schema_version' in str(caught.value)


# --------------------------------------------------------------------------
# J-N. Outcome, completeness and classification contradictions
# --------------------------------------------------------------------------
def test_partial_boundary_requires_partial_completeness():
    oracle_raw = output_oracle(RECORDS, 2, 'complete')
    case = CaseV2.model_validate(v2_case(expected_outcome='partial_boundary'))
    validate_case_semantics_v2(case)
    with pytest.raises(ValueError) as caught:
        validate_oracle_v2(case, OracleV2.model_validate(oracle_raw), oracle_raw)
    assert 'partial_boundary_requires_partial' in str(caught.value)


def test_success_rejects_partial_completeness():
    oracle_raw = output_oracle(RECORDS[:1], 1, 'partial')
    case = CaseV2.model_validate(v2_case())
    with pytest.raises(ValueError) as caught:
        validate_oracle_v2(case, OracleV2.model_validate(oracle_raw), oracle_raw)
    assert 'success_requires_terminal_completeness' in str(caught.value)


def test_partial_boundary_cannot_expect_a_terminal_failure():
    case = CaseV2.model_validate(v2_case(expected_outcome='partial_boundary',
                                         failure_expectation=unclassified_failure(),
                                         repair_expectation=unclassified_repair()))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'occurs_mismatch' in str(caught.value)


def test_unclassified_oracle_must_not_name_a_code():
    case = CaseV2.model_validate(v2_case(expected_outcome='expected_rejection', supported=False,
                                         failure_expectation=unclassified_failure(),
                                         repair_expectation=unclassified_repair(),
                                         provenance=provenance('unsupported_page_plan')))
    validate_case_semantics_v2(case)
    # 'unobserved_page_parameter' is a real SPECS code, but this failure never
    # became a PipelineError, so naming it would fake a classification.
    oracle_raw = failure_oracle('unobserved_page_parameter', 'unknown', 'ValueError')
    with pytest.raises(ValueError) as caught:
        validate_oracle_v2(case, OracleV2.model_validate(oracle_raw), oracle_raw)
    assert 'unclassified_oracle_must_not_name_code' in str(caught.value)


def test_classified_failure_rejects_an_exception_type():
    failure = {**classified_failure('duplicate_id'), 'exception_type': 'ValueError'}
    case = CaseV2.model_validate(v2_case(expected_outcome='expected_rejection',
                                         failure_expectation=failure,
                                         repair_expectation=refused_repair(
                                             'duplicate_id', RepairReason.NOT_REPAIRABLE.value),
                                         provenance=provenance('response_drift')))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'classified_exception_conflict' in str(caught.value)


def test_failure_category_must_derive_from_the_taxonomy():
    failure = {**classified_failure('duplicate_id'), 'category': Category.SECURITY.value}
    case = CaseV2.model_validate(v2_case(expected_outcome='expected_rejection',
                                         failure_expectation=failure,
                                         repair_expectation=refused_repair(
                                             'duplicate_id', RepairReason.NOT_REPAIRABLE.value),
                                         provenance=provenance('response_drift')))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'failure_classification_mismatch' in str(caught.value)


# --------------------------------------------------------------------------
# O. A plain exception message is never an error code
# --------------------------------------------------------------------------
def test_plain_exception_message_is_never_an_error_code():
    error = ValueError('unobserved_page_parameter')
    classification = classify(error)
    assert classification.category is Category.UNCLASSIFIED
    assert classification.phase == 'unknown'
    assert classification.repairable is False and classification.retryable is False
    # classify() falls back to the type name, so the message never becomes a code.
    assert classification.code == 'ValueError'
    assert classification.code != 'unobserved_page_parameter'

    plan, record = probe_plan(), probe_observation(query={'cursor': 'start'})
    with pytest.raises(ValueError) as caught:
        validate_plan(plan, record)
    assert type(caught.value) is ValueError and not isinstance(caught.value, PipelineError)
    attempt, proposal, candidate = audit(caught.value, plan, record)
    assert attempt.error_code == 'ValueError'
    assert attempt.error_category == Category.UNCLASSIFIED.value
    assert attempt.rejection_code == RepairReason.UNCLASSIFIED.value
    assert proposal is None and candidate is None

    # An unclassified failure may not name a SPECS trigger, even a real one.
    case = CaseV2.model_validate(v2_case(
        expected_outcome='expected_rejection', supported=False,
        failure_expectation=unclassified_failure(),
        repair_expectation=refused_repair('unobserved_page_parameter',
                                          RepairReason.NOT_REPAIRABLE.value),
        provenance=provenance('unsupported_page_plan')))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'unclassified_must_not_have_trigger' in str(caught.value)


# --------------------------------------------------------------------------
# P-S. Repair expectations against the real production repair layer
# --------------------------------------------------------------------------
def test_repair_pointer_applied_matches_production():
    record = probe_observation()
    plan = probe_plan(items_pointer='/missing')
    with pytest.raises(PipelineError) as caught:
        validate_plan(plan, record)
    assert caught.value.code == 'pointer_not_found'
    attempt, proposal, candidate = audit(caught.value, plan, record)
    assert (attempt.reason_code, attempt.rejection_code) == ('repairable', None)
    assert attempt.outcome == RepairOutcome.DEFERRED.value
    assert proposal.outcome == ProposalOutcome.PROPOSED.value
    assert proposal.reason_code == ProposalReason.POINTER_RELOCATED.value
    assert candidate is not None and is_applicable(caught.value, proposal)

    case = CaseV2.model_validate(v2_case(
        expected_outcome='success',
        repair_expectation=eligible_repair('pointer_not_found', ProposalOutcome.PROPOSED.value,
                                           ProposalReason.POINTER_RELOCATED.value, applied=True),
        provenance=provenance('missing_items_pointer')))
    validate_case_semantics_v2(case)
    assert case.repair_expectation.expected_applied is True


def test_repair_page_location_mismatch_is_proposal_only():
    record = probe_observation(method='POST', query={'locale': 'en'},
                               body={'page': 1, 'filter': 'active'},
                               payload={'data': {'rows': [{'id': 1, 'value': 'alpha'}],
                                                 'total': 1, 'more': False}})
    plan = probe_plan(items_pointer='/data/rows', total_pointer='/data/total',
                      has_next_pointer='/data/more', pagination_location='query')
    with pytest.raises(PipelineError) as caught:
        validate_plan(plan, record)
    assert caught.value.code == 'page_location_mismatch'
    attempt, proposal, candidate = audit(caught.value, plan, record)
    assert attempt.outcome == RepairOutcome.DEFERRED.value
    assert proposal.outcome == ProposalOutcome.PROPOSED.value
    assert proposal.reason_code == ProposalReason.PAGE_LOCATION_FROM_EVIDENCE.value
    assert candidate is not None
    # A candidate exists but the allowlist refuses to execute it.
    assert not is_applicable(caught.value, proposal)

    case = CaseV2.model_validate(v2_case(
        expected_outcome='controlled_failure',
        failure_expectation=classified_failure('page_location_mismatch', 0),
        repair_expectation=eligible_repair(
            'page_location_mismatch', ProposalOutcome.PROPOSED.value,
            ProposalReason.PAGE_LOCATION_FROM_EVIDENCE.value),
        provenance=provenance('response_drift')))
    validate_case_semantics_v2(case)
    assert case.repair_expectation.may_apply is False


def test_repair_without_a_deterministic_candidate():
    record = probe_observation(method='POST', body={'page': '1', 'filter': 'active'})
    plan = probe_plan(items_pointer='/records', pagination_location='json_body')
    with pytest.raises(PipelineError) as caught:
        validate_plan(plan, record)
    assert caught.value.code == 'invalid_page_field_type'
    attempt, proposal, candidate = audit(caught.value, plan, record)
    # Eligible and audited, yet no rule can build a safe candidate.
    assert attempt.reason_code == 'repairable' and attempt.outcome == RepairOutcome.DEFERRED.value
    assert proposal is not None and candidate is None
    assert proposal.outcome == ProposalOutcome.REJECTED.value
    assert proposal.reason_code == ProposalReason.NO_DETERMINISTIC_CANDIDATE.value

    case = CaseV2.model_validate(v2_case(
        expected_outcome='controlled_failure',
        failure_expectation=classified_failure('invalid_page_field_type', 0),
        repair_expectation=eligible_repair(
            'invalid_page_field_type', ProposalOutcome.REJECTED.value,
            ProposalReason.NO_DETERMINISTIC_CANDIDATE.value),
        provenance=provenance('response_drift')))
    validate_case_semantics_v2(case)


def test_repair_budget_exhausted_has_no_proposal_object():
    record = probe_observation()
    plan = probe_plan(items_pointer='/missing')
    with pytest.raises(PipelineError) as caught:
        validate_plan(plan, record)
    attempt, proposal, candidate = audit(caught.value, plan, record,
                                         attempts=MAX_REPAIR_ATTEMPTS)
    assert attempt.rejection_code == RepairReason.BUDGET_EXHAUSTED.value
    assert attempt.reason_code is None
    assert attempt.outcome == RepairOutcome.REJECTED.value
    # Not a rejected proposal: the proposer is never reached, so no object exists.
    assert proposal is None and candidate is None

    case = CaseV2.model_validate(v2_case(
        expected_outcome='controlled_failure',
        failure_expectation=classified_failure('pointer_not_found', 0),
        repair_expectation=refused_repair('pointer_not_found',
                                          RepairReason.BUDGET_EXHAUSTED.value),
        provenance=provenance('missing_items_pointer')))
    validate_case_semantics_v2(case)
    assert case.repair_expectation.max_attempts == 0


def test_ambiguous_pointer_yields_no_candidate():
    record = probe_observation(payload={'records': [{'id': 1, 'value': 'alpha'}],
                                        'other': [{'id': 2, 'value': 'beta'}], 'total': 1})
    plan = probe_plan(items_pointer='/missing')
    with pytest.raises(PipelineError) as caught:
        validate_plan(plan, record)
    attempt, proposal, candidate = audit(caught.value, plan, record)
    # Two lists of records means two candidates, which is not deterministic.
    assert candidate is None and proposal.outcome == ProposalOutcome.REJECTED.value
    assert proposal.reason_code == ProposalReason.NO_DETERMINISTIC_CANDIDATE.value
    assert not is_applicable(caught.value, proposal)


@pytest.mark.parametrize('mutation,error', [
    ({'expected_proposal_outcome': 'invented'}, 'proposal_outcome'),
    ({'expected_proposal_reason': 'invented'}, 'proposal_reason'),
    ({'expected_rejection_reason': 'invented'}, 'rejection_reason'),
    ({'expected_attempt_outcome': 'invented'}, 'attempt_outcome'),
    ({'policy_disposition': 'invented'}, None),
    ({'expected_proposal_outcome': 5}, 'proposal_outcome'),
])
def test_invalid_repair_enum_rejected(mutation, error):
    repair = eligible_repair('pointer_not_found', ProposalOutcome.PROPOSED.value,
                             ProposalReason.POINTER_RELOCATED.value, applied=True)
    with pytest.raises(ValueError):
        CaseV2.model_validate(v2_case(repair_expectation={**repair, **mutation},
                                      provenance=provenance('missing_items_pointer')))


def test_ineligible_repair_cannot_declare_repairable_reason():
    """REPAIRABLE is the 'is repairable' marker, never a refusal. Rejecting it is a
    shape rule, not policy: the schema does not decide which refusal reason is correct,
    only that a declared refusal is a real reason and structurally coherent."""
    case = CaseV2.model_validate(v2_case(
        expected_outcome='controlled_failure',
        failure_expectation=classified_failure('pointer_not_found', 0),
        repair_expectation=refused_repair('pointer_not_found', RepairReason.REPAIRABLE.value),
        provenance=provenance('missing_items_pointer')))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'rejection_reason_cannot_be_repairable' in str(caught.value)


def test_ineligible_repair_expresses_any_non_repairable_reason():
    """Shape, not policy: an ineligible repair may declare ANY non-REPAIRABLE reason.

    pointer_not_found is really auto_applied, so NOT_REPAIRABLE / UNCLASSIFIED are
    semantically wrong for it, yet the schema accepts them: deciding the exact reason
    is RepairPolicyChecker's job. The empirical P-S tests below pin the real checker's
    output, and the B4 harness compares that to the oracle. The schema only guarantees
    the refusal is a real reason in a coherent rejection shape.
    """
    for reason in (RepairReason.NOT_REPAIRABLE, RepairReason.BUDGET_EXHAUSTED,
                   RepairReason.COLLECTION_STARTED, RepairReason.MISSING_PLAN,
                   RepairReason.UNCLASSIFIED):
        case = CaseV2.model_validate(v2_case(
            expected_outcome='controlled_failure',
            failure_expectation=classified_failure('pointer_not_found', 0),
            repair_expectation=refused_repair('pointer_not_found', reason.value),
            provenance=provenance('missing_items_pointer')))
        validate_case_semantics_v2(case)


def test_unrepaired_trigger_cannot_claim_success():
    case = CaseV2.model_validate(v2_case(
        expected_outcome='success',
        repair_expectation=refused_repair('pointer_not_found',
                                          RepairReason.BUDGET_EXHAUSTED.value),
        provenance=provenance('missing_items_pointer')))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'unrepaired_trigger_cannot_succeed' in str(caught.value)


def test_applied_requires_may_apply_and_proposed():
    repair = {**eligible_repair('page_location_mismatch', ProposalOutcome.PROPOSED.value,
                               ProposalReason.PAGE_LOCATION_FROM_EVIDENCE.value),
              'expected_applied': True}
    case = CaseV2.model_validate(v2_case(repair_expectation=repair,
                                         provenance=provenance('response_drift'),
                                         failure_expectation=no_failure()))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'forbidden_expected_application' in str(caught.value)


# --------------------------------------------------------------------------
# Exposure model
# --------------------------------------------------------------------------
def test_exposure_must_match_split():
    case = CaseV2.model_validate(v2_case(split='author_exposed_evaluation', exposure='held',
                                         exposure_history=[{'role': 'independent_author',
                                                            'phase': 'm61_c', 'event': 'filled'}]))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'exposure_split_mismatch' in str(caught.value)


def test_held_case_requires_a_fill_event():
    case = CaseV2.model_validate(v2_case(split='future_held_evaluation', exposure='held',
                                         exposure_history=[{'role': 'auditor', 'phase': 'm61_c',
                                                            'event': 'reviewed'}]))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'held_without_fill_event' in str(caught.value)


def test_downgrade_from_held_must_be_recorded():
    history = [{'role': 'independent_author', 'phase': 'm61_c', 'event': 'filled'}]
    case = CaseV2.model_validate(v2_case(split='future_held_evaluation', exposure='author_exposed',
                                         exposure_history=history))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'unrecorded_held_downgrade' in str(caught.value)
    recorded = history + [{'role': 'auditor', 'phase': 'm61_c', 'event': 'downgraded_from_held'}]
    assert validate_case_semantics_v2(CaseV2.model_validate(v2_case(
        split='future_held_evaluation', exposure='author_exposed',
        exposure_history=recorded))) is None


def test_exposure_state_machine_is_one_way():
    """held_reserved -> held -> author_exposed is irreversible (BLOCKER 2).

    A. clean held: filled, never downgraded -> PASS.
    B. held -> author_exposed: downgrade recorded -> PASS.
    C. still 'held' yet shows a downgrade -> the case was exposed then reverted, an
       irreversible leak the contract must reject as forbidden_exposure_reversal.
    """
    filled = [{'role': 'independent_author', 'phase': 'm61_c', 'event': 'filled'}]
    downgraded = filled + [{'role': 'auditor', 'phase': 'm61_c', 'event': 'downgraded_from_held'}]

    # A. clean held
    assert validate_case_semantics_v2(CaseV2.model_validate(v2_case(
        split='future_held_evaluation', exposure='held', exposure_history=filled))) is None

    # B. held -> author_exposed, downgrade recorded
    assert validate_case_semantics_v2(CaseV2.model_validate(v2_case(
        split='future_held_evaluation', exposure='author_exposed',
        exposure_history=downgraded))) is None

    # C. forbidden reversal: still 'held' but history shows a downgrade
    case = CaseV2.model_validate(v2_case(
        split='future_held_evaluation', exposure='held', exposure_history=downgraded))
    with pytest.raises(ValueError) as caught:
        validate_case_semantics_v2(case)
    assert 'forbidden_exposure_reversal' in str(caught.value)


def test_exposure_event_cannot_carry_identity():
    for extra in ({'name': 'someone'}, {'email': 'a@b.c'}, {'account': 'x'}, {'note': 'free text'}):
        with pytest.raises(ValueError):
            ExposureEvent.model_validate({'role': 'auditor', 'phase': 'm61_b1',
                                          'event': 'reviewed', **extra})
    with pytest.raises(ValueError):
        CaseV2.model_validate(v2_case(exposure_history=[]))
    with pytest.raises(ValueError):
        ExposureEvent.model_validate({'role': 'manager', 'phase': 'm61_b1', 'event': 'reviewed'})


# --------------------------------------------------------------------------
# U. Enum drift guard: production stays the only authority
# --------------------------------------------------------------------------
def test_production_policy_snapshot_forces_review_on_drift():
    # A snapshot, not a second policy source: if production changes, this fails and
    # a human must re-read the evaluation contract instead of it passing silently.
    assert {entry.value for entry in ProposalOutcome} == {'proposed', 'rejected'}
    assert {entry.value for entry in ProposalReason} == {
        'page_location_from_evidence', 'pointer_relocated', 'no_deterministic_candidate'}
    assert {entry.value for entry in RepairReason} == {
        'repairable', 'unclassified', 'not_repairable', 'repair_budget_exhausted',
        'collection_started', 'missing_original_plan'}
    assert {entry.value for entry in RepairOutcome} == {'rejected', 'deferred'}
    assert set(DISPOSITIONS) == {'auto_applied', 'proposal_only', 'rejected'}
    assert APPLICABLE_ERROR_CODES == frozenset({'pointer_not_found'})
    assert MAX_REPAIR_ATTEMPTS == 1
    assert set(DEFAULT_GENERATORS) == {'pointer_not_found', 'page_location_mismatch',
                                       'invalid_page_field_type'}


def test_published_v2_schema_binds_production_enums():
    schema = json.loads((ROOT / CASE_V2_SCHEMA_REF).read_text(encoding='utf-8'))
    assert schema == CaseV2.model_json_schema()
    assert schema['additionalProperties'] is False
    definitions = schema['$defs']
    for name, enum_class in (('ProposalOutcome', ProposalOutcome), ('ProposalReason', ProposalReason),
                             ('RepairReason', RepairReason), ('RepairOutcome', RepairOutcome),
                             ('Category', Category)):
        # The closed set is emitted from the production enum, never retyped here.
        assert definitions[name]['enum'] == [entry.value for entry in enum_class], name
    assert definitions['RepairExpectationV2']['additionalProperties'] is False
    assert definitions['ExposureEvent']['additionalProperties'] is False


def test_disposition_vocabulary_is_projected_not_copied():
    """policy_disposition is a read-only projection of the production taxonomy (rule B),
    never a copied vocabulary. The exact RepairReason is deliberately NOT projected here
    any more: that is RepairPolicyChecker's decision, pinned only by the empirical P-S
    tests above and compared to the oracle by the B4 harness."""
    assert project_repairability('pointer_not_found') == 'auto_applied'
    assert project_repairability('page_location_mismatch') == 'proposal_only'
    assert project_repairability('invalid_page_field_type') == 'proposal_only'
    assert project_repairability('duplicate_id') == 'rejected'
    assert project_repairability('unobserved_page_parameter') == 'proposal_only'


# --------------------------------------------------------------------------
# V. M6.1-A byte/hash freeze guard
# --------------------------------------------------------------------------
def test_m61a_freeze_guard_passes_on_untouched_tree():
    assert assert_m61a_frozen(ROOT) == 12


def test_no_unindexed_json_inside_the_a_scanned_directories():
    """The frozen A validator rglobs three directories and rejects any JSON its own
    manifest does not index, so B assets must never be added inside them. This is why
    the published V2 schema lives at CASE_V2_SCHEMA_REF rather than in schemas/.
    """
    assert sorted(p.name for p in (ROOT / 'evaluation/schemas').rglob('*.json')) == ['case.schema.json']
    assert sorted(p.name for p in (ROOT / 'evaluation/datasets/m6-v1').rglob('*.json'))
    assert sorted(p.name for p in (ROOT / 'evaluation/corpora/m6-v1').rglob('*.json')) == [
        'e1.json', 'e2.json', 'e3.json']
    assert CASE_V2_SCHEMA_REF == 'evaluation/v2/case_v2.schema.json'
    assert sorted(p.name for p in (ROOT / 'evaluation/v2').rglob('*.json')) == ['case_v2.schema.json']


def bundle_copy(tmp_path):
    shutil.copytree(ROOT / 'evaluation', tmp_path / 'evaluation')
    return tmp_path


def reseal(root):
    """Recompute every hash so a tampered bundle becomes self-consistent again."""
    base = root / 'evaluation/datasets/m6-v1'
    manifest = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
    for ref in manifest['files']:
        manifest['files'][ref] = digest(json.loads((root / ref).read_text(encoding='utf-8')))
    for ref in manifest['sources']:
        manifest['sources'][ref] = source_digest((root / ref).read_text(encoding='utf-8'))
    (base / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
        encoding='utf-8', newline='\n')
    (base / 'manifest.sha256').write_text(digest(manifest) + '\n', encoding='utf-8', newline='\n')


def test_freeze_guard_detects_source_tampering(tmp_path):
    root = bundle_copy(tmp_path)
    path = root / 'evaluation/schemas/scoring.py'
    path.write_text(path.read_text(encoding='utf-8') + '\n# tampered\n', encoding='utf-8')
    with pytest.raises(ValueError):
        assert_m61a_frozen(root)


def test_freeze_guard_detects_asset_tampering(tmp_path):
    root = bundle_copy(tmp_path)
    path = root / 'evaluation/datasets/m6-v1/oracles/m6_get_flat.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['record_count'] = 99
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
                    encoding='utf-8', newline='\n')
    with pytest.raises(ValueError):
        assert_m61a_frozen(root)


def test_freeze_guard_rejects_a_resealed_manifest(tmp_path):
    """The decisive check: self-consistency alone must not pass as frozen."""
    root = bundle_copy(tmp_path)
    path = root / 'evaluation/schemas/scoring.py'
    path.write_text(path.read_text(encoding='utf-8') + '\n# tampered\n', encoding='utf-8')
    reseal(root)
    # validate_bundle only proves internal consistency, so it is satisfied here.
    assert len(validate_bundle(root)) == 12
    with pytest.raises(ValueError) as caught:
        assert_m61a_frozen(root)
    assert 'm61a_manifest_not_frozen' in str(caught.value)


# --------------------------------------------------------------------------
# Model-level fail-closed behaviour
# --------------------------------------------------------------------------
@pytest.mark.parametrize('field,value', [
    ('split', 'frozen_evaluation'), ('split', 'test'), ('exposure', 'held_reserved'),
    ('expected_outcome', 'maybe'), ('evidence_level', 'live_cloud'),
    ('categories', ['invented']), ('categories', []), ('version', 1), ('version', True),
    ('id', '../escape'), ('extra', 'forbidden'),
])
def test_case_v2_rejects_invalid_fields(field, value):
    data = {**v2_case(), field: value}
    if field == 'extra':
        data = {**v2_case(), 'extra': 'forbidden'}
    with pytest.raises(ValueError):
        CaseV2.model_validate(data)


@pytest.mark.parametrize('value', [2.0, True, '2', 1, 3])
def test_case_v2_version_must_be_strict_integer_two(value):
    """V2 is as strict as V1: only the real integer 2 validates. 2.0, True and '2' are
    rejected rather than silently coerced, so a float/bool/str version cannot sneak
    through the model itself even before dispatch_case guards the same boundary."""
    with pytest.raises(ValueError):
        CaseV2.model_validate({**v2_case(), 'version': value})


def test_case_v2_version_two_is_accepted():
    assert CaseV2.model_validate({**v2_case(), 'version': 2}).version == 2


def test_case_v2_rejects_nested_extra_and_bad_hashes():
    with pytest.raises(ValueError):
        CaseV2.model_validate({**v2_case(), 'oracle': {'ref': ORACLE_REF, 'extra': 1}})
    with pytest.raises(ValueError):
        CaseV2.model_validate({**v2_case(), 'hashes': {'fixture': 'zz', 'oracle': HASH,
                                                       'retrieval_labels': HASH}})
    with pytest.raises(ValueError):
        CaseV2.model_validate({**v2_case(), 'fixture': {**v2_case()['fixture'],
                                                        'generator_version': 'm61-a-1'}})


def test_oracle_v2_rejects_records_hash_mismatch():
    oracle_raw = output_oracle(RECORDS, 2, 'complete')
    oracle_raw['record_count'] = 1
    case = CaseV2.model_validate(v2_case())
    with pytest.raises(ValueError) as caught:
        validate_oracle_v2(case, OracleV2.model_validate(oracle_raw), oracle_raw)
    assert 'oracle_records_hash_mismatch' in str(caught.value)


def test_failure_oracle_must_not_carry_records():
    oracle_raw = {**failure_oracle('duplicate_id', 'executing', None), 'records': RECORDS}
    case = CaseV2.model_validate(v2_case(
        expected_outcome='expected_rejection', failure_expectation=classified_failure('duplicate_id'),
        repair_expectation=refused_repair('duplicate_id', RepairReason.NOT_REPAIRABLE.value),
        provenance=provenance('response_drift')))
    with pytest.raises(ValueError) as caught:
        validate_oracle_v2(case, OracleV2.model_validate(oracle_raw), oracle_raw)
    assert 'failure_oracle_must_be_empty' in str(caught.value)
