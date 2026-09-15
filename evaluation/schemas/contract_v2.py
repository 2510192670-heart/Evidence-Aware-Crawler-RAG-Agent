"""M6.1-B1 evaluation-only Case Schema V2, version dispatch and A freeze guard.

V2 exists because ``contract.Case.version`` is pinned to ``le=1``: adding fields
to V1 would either migrate the frozen ``m6-v1`` bundle or break its published
schema, so V2 is a parallel model instead. V1 cases keep travelling through
``contract.py`` untouched and the five source-hashed A files stay byte-identical.

Policy is never re-declared here: this module validates expectation *shape* only.
Dispositions come from ``knowledge.project_repairability``, the apply allowlist
and the attempt budget come from ``repair``, categories and phases come from
``errors.SPECS``, and the repair enums are bound directly so no second string
vocabulary can drift. Which exact ``RepairReason`` a failure deserves is decided
solely by ``repair.RepairPolicyChecker``; the B4 harness runs that real checker and
compares it to the oracle, so no refusal-priority tree is mirrored here.

Strict mode only accepts real enum instances, so canonical JSON values are
coerced through the production enum constructor. That keeps the closed set owned
by ``repair.py`` while still failing closed on an unknown value.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StrictInt

from backend.app.pipeline.errors import SPECS
from backend.app.pipeline.errors import Category as ErrorCategory
from backend.app.pipeline.repair import (APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS,
                                         ProposalOutcome, ProposalReason, RepairOutcome,
                                         RepairReason)
from backend.app.rag.knowledge import project_repairability

from .contract import (ID, SHA, Case, Closed, Hashes, Provenance, Ref, Request,
                       read_json, validate_bundle, validate_case_semantics)
from .contract import Category as CaseCategory
from .hashing import digest
from .scoring import score_task_result

__all__ = ['CASE_SCHEMA_VERSIONS', 'CASE_V2_SCHEMA_REF', 'CaseV2', 'ExposureEvent',
           'FailureExpectationV2', 'FixtureV2', 'M61A_BASE', 'M61A_MANIFEST_SHA256',
           'M61A_SOURCES', 'OracleV2', 'RepairExpectationV2', 'SPLIT_EXPOSURE',
           'TERMINAL_FAILURE_OUTCOMES', 'assert_m61a_frozen', 'dispatch_case',
           'validate_case_semantics_v2', 'validate_oracle_v2']

CASE_SCHEMA_VERSIONS = (1, 2)
M61A_BASE = 'evaluation/datasets/m6-v1/'
# Pinned inventory: validate_bundle already hashes these, this only proves the set
# itself was not widened or narrowed to hide a change.
M61A_SOURCES = ('evaluation/fixtures/build_dataset.py', 'evaluation/fixtures/runtime.py',
                'evaluation/schemas/contract.py', 'evaluation/schemas/hashing.py',
                'evaluation/schemas/scoring.py')
# Recorded at the M6.1-A freeze (commit 18bd7d5). This digest covers every file and
# source hash inside the A manifest, so pinning it pins the whole bundle.
M61A_MANIFEST_SHA256 = '8cbe2d165b789a581baf5e21b8568dba1d4fe0a788abc188fc377f646cc95313'
# The published V2 schema must NOT sit in evaluation/schemas/: the frozen A validator
# rglobs that directory and rejects any JSON its own manifest does not index, so
# placing it there would break m6-v1 validation. B5 indexes this path from m6-v1b.
CASE_V2_SCHEMA_REF = 'evaluation/v2/case_v2.schema.json'

# Only these two outcomes end in a terminal exception; a partial page budget is a
# normal return and must never be scored as either success or failure.
TERMINAL_FAILURE_OUTCOMES = frozenset({'expected_rejection', 'controlled_failure'})

# Exposure is a one-way state machine, so a split admits only the states that can
# legitimately precede it. 'held_reserved' belongs to reservations, never to a case.
SPLIT_EXPOSURE = {
    'development': frozenset({'author_exposed'}),
    'author_exposed_evaluation': frozenset({'author_exposed'}),
    'future_held_evaluation': frozenset({'held', 'author_exposed'}),
}


def _enum_member(enum_class, field_name):
    """Coerce a canonical value into its production enum member, or fail closed."""

    def coerce(value):
        if value is None or isinstance(value, enum_class):
            return value
        if not isinstance(value, str):
            raise ValueError('invalid_' + field_name)
        try:
            return enum_class(value)
        except ValueError:
            raise ValueError('invalid_' + field_name) from None

    return BeforeValidator(coerce)


class ExposureEvent(Closed):
    """One exposure audit entry: closed to three enums so no PII is representable."""

    role: Literal['dataset_author', 'harness_author', 'auditor', 'independent_author', 'scorer']
    phase: Literal['m61_a', 'm61_b1', 'm61_b2', 'm61_b3', 'm61_b4', 'm61_b5', 'm61_b6',
                   'm61_b7', 'm61_c']
    event: Literal['authored', 'reviewed', 'audited', 'filled', 'scored', 'downgraded_from_held']


class FixtureV2(Ref):
    fixture_id: str = Field(pattern=ID)
    generator_version: Literal['m61-b1-1']


class FailureExpectationV2(Closed):
    """Terminal-failure expectation; ``occurs`` separates a return from a raise.

    V1 inferred this from ``expected_outcome``, which cannot express a partial
    page budget: that outcome neither succeeds nor raises.
    """

    occurs: bool
    classified: bool
    allowed_codes: list[str]
    category: Annotated[ErrorCategory | None, _enum_member(ErrorCategory, 'category')]
    phase: Literal['observing', 'analyzing', 'executing', 'unknown'] | None
    exception_type: Literal['ValueError'] | None
    max_execution_requests: StrictInt = Field(ge=0, le=20)


class RepairExpectationV2(Closed):
    """Bounded-repair expectation bound directly to the production enums.

    ``expected_proposal_outcome is None`` means no proposal object exists at all
    (an ineligible failure never reaches the proposer), which is a different fact
    from a proposal that exists and was rejected for want of a candidate.
    """

    trigger_code: str | None
    policy_disposition: Literal['none', 'auto_applied', 'proposal_only', 'rejected']
    eligible: bool
    expected_rejection_reason: Annotated[RepairReason | None,
                                         _enum_member(RepairReason, 'rejection_reason')]
    expected_proposal_outcome: Annotated[ProposalOutcome | None,
                                         _enum_member(ProposalOutcome, 'proposal_outcome')]
    expected_proposal_reason: Annotated[ProposalReason | None,
                                        _enum_member(ProposalReason, 'proposal_reason')]
    expected_attempt_outcome: Annotated[RepairOutcome | None,
                                        _enum_member(RepairOutcome, 'attempt_outcome')]
    may_apply: bool
    expected_applied: bool
    max_attempts: StrictInt = Field(ge=0, le=1)


class OracleV2(Closed):
    """Scorer-only answer. ``completeness`` mirrors the three executor outcomes."""

    records: list[dict] | None
    page_count: StrictInt | None = Field(ge=1, le=10)
    record_count: StrictInt | None = Field(ge=0)
    result_sha256: str | None = Field(pattern=SHA)
    output_schema: dict[str, Literal['integer', 'number', 'string', 'boolean']] = Field(alias='schema')
    unique_key: str | None
    completeness: Literal['complete', 'stop_condition_only', 'partial'] | None
    expected_error_code: str | None
    expected_error_phase: Literal['observing', 'analyzing', 'executing', 'unknown'] | None
    expected_exception_type: Literal['ValueError'] | None


class CaseV2(Closed):
    id: str = Field(pattern=ID)
    family_id: str = Field(pattern=ID)
    # StrictInt mirrors V1's ``version`` strictness: 2.0, True and "2" are all rejected
    # rather than silently coerced, so the only document that validates is a real int 2.
    version: StrictInt = Field(ge=2, le=2)
    split: Literal['development', 'author_exposed_evaluation', 'future_held_evaluation']
    categories: list[CaseCategory] = Field(min_length=1)
    evidence_level: Literal['component_fixture']
    supported: bool
    expected_outcome: Literal['success', 'partial_boundary', 'expected_rejection',
                              'controlled_failure']
    exposure: Literal['author_exposed', 'held']
    exposure_history: list[ExposureEvent] = Field(min_length=1)
    fixture: FixtureV2
    request: Request
    oracle: Ref
    retrieval_labels: Ref
    failure_expectation: FailureExpectationV2
    repair_expectation: RepairExpectationV2
    provenance: Provenance
    hashes: Hashes


def _validate_refusal_shape(repair):
    """Rule F: a refused repair carries a real, non-REPAIRABLE reason in rejection shape.

    Which exact ``RepairReason`` a failure deserves is decided solely by
    ``repair.RepairPolicyChecker``. This helper therefore never derives a reason from
    the trigger or its context; it only rejects a value that cannot be a refusal at all
    (``REPAIRABLE``) or a shape that contradicts "ineligible and refused". The B4
    harness runs the real checker and compares its reason to the oracle expectation.
    """
    if repair.expected_rejection_reason is None:
        raise ValueError('missing_rejection_reason')
    if repair.expected_rejection_reason is RepairReason.REPAIRABLE:
        raise ValueError('rejection_reason_cannot_be_repairable')
    if repair.expected_attempt_outcome is not RepairOutcome.REJECTED \
            or repair.expected_proposal_outcome is not None \
            or repair.expected_proposal_reason is not None or repair.max_attempts:
        raise ValueError('ineligible_repair_mismatch')


def _validate_exposure_v2(case):
    if case.exposure not in SPLIT_EXPOSURE[case.split]:
        raise ValueError('exposure_split_mismatch')
    events = [entry.event for entry in case.exposure_history]
    # The state machine is one-way (held_reserved -> held -> author_exposed), so a case
    # still marked 'held' must never show a downgrade event: that would mean it was
    # exposed and then reverted, an irreversible leak the contract has to reject.
    if case.exposure == 'held' and 'downgraded_from_held' in events:
        raise ValueError('forbidden_exposure_reversal')
    # A held case that was later seen by an A/B author must say so, one-way.
    if case.exposure == 'author_exposed' and 'filled' in events \
            and 'downgraded_from_held' not in events:
        raise ValueError('unrecorded_held_downgrade')
    if case.exposure == 'held' and 'filled' not in events:
        raise ValueError('held_without_fill_event')


def _validate_failure_v2(case, failure):
    if failure.occurs != (case.expected_outcome in TERMINAL_FAILURE_OUTCOMES):
        raise ValueError('occurs_mismatch')
    codes = failure.allowed_codes
    if len(set(codes)) != len(codes) or any(code not in SPECS for code in codes):
        raise ValueError('invalid_error_code')
    if failure.classified != bool(codes):
        raise ValueError('classified_codes_mismatch')
    if not failure.occurs:
        # success and partial_boundary both return normally.
        if failure.classified or failure.category is not None or failure.phase is not None \
                or failure.exception_type is not None:
            raise ValueError('normal_return_must_not_expect_failure')
    elif failure.classified:
        if failure.exception_type is not None:
            raise ValueError('classified_exception_conflict')
        if any(SPECS[code].category is not failure.category or SPECS[code].phase != failure.phase
               for code in codes):
            raise ValueError('failure_classification_mismatch')
    elif failure.category is not ErrorCategory.UNCLASSIFIED or failure.phase != 'unknown' \
            or failure.exception_type != 'ValueError':
        raise ValueError('invalid_unclassified_expectation')


def _validate_repair_v2(case, failure, repair):
    trigger = repair.trigger_code
    if trigger is not None and trigger not in SPECS:
        raise ValueError('invalid_trigger_code')
    if failure.classified:
        if trigger not in failure.allowed_codes:
            raise ValueError('trigger_code_not_allowed')
    elif failure.occurs and trigger is not None:
        # A terminal unclassified failure carries no SPECS code, so it cannot name a
        # trigger. Its message is never read as a code either. A case that returns
        # normally is exempt: there the trigger records the failure a bounded repair
        # already resolved, which is how an applied pointer relocation stays expressible.
        raise ValueError('unclassified_must_not_have_trigger')
    if trigger is not None and case.provenance.injection == 'none':
        raise ValueError('missing_failure_provenance')
    if not failure.occurs and case.expected_outcome == 'partial_boundary' and trigger is not None:
        raise ValueError('repair_trigger_without_failure')

    if trigger is None:
        if repair.policy_disposition != 'none' or repair.may_apply or repair.expected_applied:
            raise ValueError('repair_without_trigger')
        if not failure.occurs:
            # Nothing failed, so the repair layer is never consulted.
            if repair.eligible or repair.expected_rejection_reason is not None \
                    or repair.expected_attempt_outcome is not None \
                    or repair.expected_proposal_outcome is not None \
                    or repair.expected_proposal_reason is not None or repair.max_attempts:
                raise ValueError('repair_expectation_without_failure')
            return
        # An unclassified terminal failure has no SPECS code, so it can never be
        # eligible; it is refused. Only the refusal shape is checked here (rule F),
        # never the exact reason, which RepairPolicyChecker alone decides.
        if repair.eligible:
            raise ValueError('unclassified_cannot_be_eligible')
        _validate_refusal_shape(repair)
        return

    # A case that returns normally while naming a trigger claims the bounded repair
    # resolved it; anything else is a contradiction.
    if not failure.occurs and not (repair.eligible and repair.expected_applied):
        raise ValueError('unrepaired_trigger_cannot_succeed')

    if repair.policy_disposition != project_repairability(trigger):
        raise ValueError('repair_policy_mismatch')
    if repair.may_apply != (trigger in APPLICABLE_ERROR_CODES):
        raise ValueError('repair_apply_mismatch')
    if repair.expected_applied and not (repair.may_apply and repair.eligible):
        raise ValueError('forbidden_expected_application')

    if not repair.eligible:
        # Rule F: shape only. The exact RepairReason is the checker's decision and is
        # compared against the oracle by the B4 harness, never re-derived here.
        _validate_refusal_shape(repair)
        return

    if repair.expected_rejection_reason is not None \
            or repair.expected_attempt_outcome is not RepairOutcome.DEFERRED \
            or repair.expected_proposal_outcome is None \
            or repair.expected_proposal_reason is None \
            or repair.max_attempts != MAX_REPAIR_ATTEMPTS:
        raise ValueError('eligible_repair_mismatch')
    if repair.expected_applied and repair.expected_proposal_outcome is not ProposalOutcome.PROPOSED:
        raise ValueError('applied_requires_proposed')


def validate_case_semantics_v2(case):
    """Intra-case V2 consistency. Cross-file checks live in ``validate_oracle_v2``."""
    failure, repair = case.failure_expectation, case.repair_expectation
    if len(set(case.categories)) != len(case.categories):
        raise ValueError('duplicate_category')
    _validate_exposure_v2(case)
    _validate_failure_v2(case, failure)
    if not case.supported and case.expected_outcome != 'expected_rejection':
        raise ValueError('unsupported_must_reject')
    if case.provenance.injected_failure != (case.provenance.injection != 'none'):
        raise ValueError('injection_provenance_mismatch')
    _validate_repair_v2(case, failure, repair)


def validate_oracle_v2(case, oracle, oracle_raw):
    """Cross-check a V2 case against its scorer-only oracle document."""
    outcome, failure = case.expected_outcome, case.failure_expectation
    if outcome in TERMINAL_FAILURE_OUTCOMES:
        if oracle.records is not None or oracle.page_count is not None \
                or oracle.record_count is not None or oracle.result_sha256 is not None \
                or oracle.completeness is not None or oracle.unique_key is not None \
                or oracle.output_schema:
            raise ValueError('failure_oracle_must_be_empty')
        if failure.classified:
            if oracle.expected_error_code not in failure.allowed_codes:
                raise ValueError('oracle_error_code_mismatch')
        elif oracle.expected_error_code is not None:
            # The message of a plain exception is never promoted to an error code.
            raise ValueError('unclassified_oracle_must_not_name_code')
        if oracle.expected_error_phase != failure.phase \
                or oracle.expected_exception_type != failure.exception_type:
            raise ValueError('oracle_failure_expectation_mismatch')
        return

    if oracle.expected_error_code is not None or oracle.expected_error_phase is not None \
            or oracle.expected_exception_type is not None:
        raise ValueError('output_oracle_must_not_expect_error')
    if oracle.records is None or oracle.result_sha256 is None or oracle.page_count is None \
            or oracle.record_count is None or not oracle.output_schema \
            or oracle.unique_key not in oracle.output_schema:
        raise ValueError('incomplete_output_oracle')
    if oracle.record_count != len(oracle.records) or digest(oracle.records) != oracle.result_sha256:
        raise ValueError('oracle_records_hash_mismatch')
    if outcome == 'partial_boundary':
        if oracle.completeness != 'partial':
            raise ValueError('partial_boundary_requires_partial')
        # NOTE: score_task_result only checks report/oracle agreement, so a partial
        # oracle does satisfy it when the report also says 'partial'. Isolating a
        # partial boundary from Task Success Rate is therefore the metric layer's
        # job (B6 must dispatch on expected_outcome), and cannot be enforced here.
        return
    if oracle.completeness not in ('complete', 'stop_condition_only'):
        raise ValueError('success_requires_terminal_completeness')
    if not score_task_result({'status': 'succeeded', 'pages': oracle.page_count,
                              'completeness': oracle.completeness}, oracle.records,
                             oracle_raw)['task_success']:
        raise ValueError('invalid_success_oracle')


def dispatch_case(raw, declared_version):
    """Route one raw case document to its version contract; never mix versions.

    ``type(version) is not int`` rejects ``true``, which would otherwise compare
    equal to 1 and silently travel the V1 path.
    """
    if declared_version not in CASE_SCHEMA_VERSIONS or type(declared_version) is not int:
        raise ValueError('unknown_case_schema_version')
    version = raw.get('version') if isinstance(raw, dict) else None
    if type(version) is not int or version not in CASE_SCHEMA_VERSIONS:
        raise ValueError('unknown_case_version')
    if version != declared_version:
        raise ValueError('mixed_case_schema_version')
    if version == 1:
        case = Case.model_validate(raw)
        validate_case_semantics(case)
        return case
    case = CaseV2.model_validate(raw)
    validate_case_semantics_v2(case)
    return case


def assert_m61a_frozen(root):
    """Prove the frozen A bundle is byte-identical, not merely self-consistent.

    ``validate_bundle`` stays the hashing authority, but it only proves internal
    consistency: a source file edited together with a regenerated manifest would
    satisfy it. Pinning the digest recorded at the M6.1-A freeze is what makes this
    guard independent, and because that digest covers every file and source hash in
    the manifest, one constant pins the whole bundle. No hashing rule is duplicated.
    """
    cases = validate_bundle(root)
    recorded = (Path(root) / (M61A_BASE + 'manifest.sha256')).read_text(encoding='utf-8').strip()
    if recorded != M61A_MANIFEST_SHA256:
        raise ValueError('m61a_manifest_not_frozen')
    manifest = read_json(root, M61A_BASE + 'manifest.json')
    if tuple(sorted(manifest['sources'])) != M61A_SOURCES:
        raise ValueError('a_source_inventory_changed')
    return len(cases)
