"""Bounded repair: eligibility policy, deterministic proposals and audit.

M4.4.1 established the contract, the fail-closed policy and the audit record.
M4.4.2 adds deterministic candidate generation: for an eligible failure the
module may build a *candidate* plan from the already-observed evidence and run
it through the same pre-flight validator the executor uses.

This module still performs no repair. It never mutates the original plan, never
retries execution, never re-observes the target and never calls the model. A
proposal is evidence, not an action.

Policy is fail closed:

* the error registry in ``errors.py`` stays the single source of truth for
  what is repairable; this module never re-declares category policy,
* an unknown code stays ``UNCLASSIFIED`` and is rejected,
* a failure that may have begun collection is rejected,
* a missing original plan or an exhausted attempt budget is rejected,
* only errors with a registered deterministic rule can ever yield a candidate;
  everything else is rejected.

Every field of :class:`RepairAttempt` and :class:`RepairProposal` is produced by
program code; nothing is read from model output, and no response value is
persisted -- proposals carry hashes, plan field names and codes only.
"""

import re
from dataclasses import dataclass
from enum import Enum

from .contracts import SENSITIVE, plan_hash, validate_plan
from .errors import Category, classify

__all__ = ['MAX_CANDIDATE_MEMBERS', 'MAX_REPAIR_ATTEMPTS', 'PRE_COLLECTION_PHASES',
           'ProposalOutcome', 'ProposalReason', 'RepairAttempt', 'RepairContext',
           'RepairDecision', 'RepairOutcome', 'RepairPolicyChecker', 'RepairProposal',
           'RepairProposer', 'RepairReason', 'audit_document', 'audit_failure', 'build_attempt',
           'is_collection_started', 'propose']

# Bounded repair: at most one plan repair per task. M4.4.1 never spends it.
MAX_REPAIR_ATTEMPTS = 1
# 观察/分析阶段失败时尚无采集产物，确定性修复才是安全的；其余阶段（含 unknown）一律
# 视为可能已开始采集，fail closed。
PRE_COLLECTION_PHASES = frozenset({'observing', 'analyzing'})
# 候选指针搜索只看已观察响应体的直接成员，并对成员数封顶：范围有限才能保证确定性与
# 无歧义，深层结构留给后续里程碑。
MAX_CANDIDATE_MEMBERS = 200


class RepairOutcome(str, Enum):
    """Disposition of one audited repair attempt."""

    # 策略拒绝：不允许修复，rejection_code 说明原因。
    REJECTED = 'rejected'
    # 策略允许，但本里程碑不执行任何修复（基础设施阶段）。
    DEFERRED = 'deferred'


class RepairReason(str, Enum):
    """Closed registry of repair policy decisions; never model-provided."""

    REPAIRABLE = 'repairable'
    UNCLASSIFIED = 'unclassified'
    NOT_REPAIRABLE = 'not_repairable'
    BUDGET_EXHAUSTED = 'repair_budget_exhausted'
    COLLECTION_STARTED = 'collection_started'
    MISSING_PLAN = 'missing_original_plan'


_REASONS = frozenset(reason.value for reason in RepairReason)
_OUTCOMES = frozenset(outcome.value for outcome in RepairOutcome)


class ProposalOutcome(str, Enum):
    """Disposition of a deterministic candidate proposal (never an execution)."""

    # 已生成候选且通过执行前校验，等待未来里程碑决定是否应用。
    PROPOSED = 'proposed'
    # 无法生成候选，或候选未通过校验。
    REJECTED = 'rejected'


class ProposalReason(str, Enum):
    """Closed registry of deterministic proposal grounds; never model-provided."""

    PAGE_LOCATION_FROM_EVIDENCE = 'page_location_from_evidence'
    POINTER_RELOCATED = 'pointer_relocated'
    NO_DETERMINISTIC_CANDIDATE = 'no_deterministic_candidate'


_PROPOSAL_REASONS = frozenset(reason.value for reason in ProposalReason)
_PROPOSAL_OUTCOMES = frozenset(outcome.value for outcome in ProposalOutcome)


@dataclass(frozen=True)
class RepairDecision:
    """Result of the eligibility check: allowed or rejected, with one reason."""

    eligible: bool
    reason: RepairReason


@dataclass(frozen=True)
class RepairContext:
    """Program-derived facts about a failure; never model output or response data."""

    original_plan_hash: str | None = None
    attempts: int = 0


@dataclass(frozen=True)
class RepairAttempt:
    """Immutable audit record for one repair decision.

    All values are program-generated. ``reason_code`` and ``rejection_code`` are
    mutually exclusive: an allowed repair carries the positive policy basis, a
    rejected one carries the refusal code. When policy allows a repair, the
    optional :class:`RepairProposal` fills ``candidate_plan_hash``,
    ``modified_fields`` and ``validation_result``; without one they stay empty.
    """

    attempt: int
    error_code: str
    error_category: str
    failure_phase: str
    collection_started: bool
    original_plan_hash: str | None
    candidate_plan_hash: str | None
    modified_fields: tuple[str, ...]
    reason_code: str | None
    validation_result: str | None
    rejection_code: str | None
    outcome: str

    def __post_init__(self):
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError('invalid_repair_attempt')
        if self.error_category not in {category.value for category in Category}:
            raise ValueError('invalid_repair_error_category')
        if type(self.collection_started) is not bool:
            raise ValueError('invalid_repair_collection_started')
        if self.outcome not in _OUTCOMES:
            raise ValueError('invalid_repair_outcome')
        if self.reason_code is not None and self.reason_code not in _REASONS:
            raise ValueError('invalid_repair_reason')
        if self.rejection_code is not None and self.rejection_code not in _REASONS:
            raise ValueError('invalid_repair_rejection')
        # 恰好一个：允许则给理由，拒绝则给拒绝码；两者皆空或皆有都是编程错误。
        if (self.reason_code is None) == (self.rejection_code is None):
            raise ValueError('repair_decision_requires_exactly_one_code')

    def to_dict(self) -> dict:
        """JSON-safe representation with a stable key order."""
        return {
            'attempt': self.attempt,
            'error_code': self.error_code,
            'error_category': self.error_category,
            'failure_phase': self.failure_phase,
            'collection_started': self.collection_started,
            'original_plan_hash': self.original_plan_hash,
            'candidate_plan_hash': self.candidate_plan_hash,
            'modified_fields': list(self.modified_fields),
            'reason_code': self.reason_code,
            'validation_result': self.validation_result,
            'rejection_code': self.rejection_code,
            'outcome': self.outcome,
        }


@dataclass(frozen=True)
class RepairProposal:
    """Immutable audit record for one deterministic candidate proposal.

    The plan itself is never stored: a proposal carries the two plan hashes, the
    plan field names a rule touched, the rule's closed reason code, the pre-flight
    validation result and the outcome. ``candidate_plan_hash`` therefore
    identifies the candidate without persisting any response structure.

    Invariant: ``outcome == proposed`` exactly when a candidate was produced and
    passed ``validate_plan`` (``validation_result == 'valid'``).
    """

    original_plan_hash: str
    candidate_plan_hash: str | None
    modified_fields: tuple[str, ...]
    reason_code: str
    validation_result: str | None
    outcome: str

    def __post_init__(self):
        if not isinstance(self.original_plan_hash, str) or not self.original_plan_hash:
            raise ValueError('invalid_proposal_original_plan_hash')
        if self.reason_code not in _PROPOSAL_REASONS:
            raise ValueError('invalid_proposal_reason')
        if self.outcome not in _PROPOSAL_OUTCOMES:
            raise ValueError('invalid_proposal_outcome')
        if self.validation_result is not None and self.validation_result != 'valid' \
                and not self.validation_result.startswith('invalid:'):
            raise ValueError('invalid_proposal_validation_result')
        proposed = self.outcome == ProposalOutcome.PROPOSED.value
        if proposed != (self.candidate_plan_hash is not None and self.validation_result == 'valid'):
            raise ValueError('proposal_outcome_inconsistent')

    def to_dict(self) -> dict:
        """JSON-safe representation with a stable key order."""
        return {
            'original_plan_hash': self.original_plan_hash,
            'candidate_plan_hash': self.candidate_plan_hash,
            'modified_fields': list(self.modified_fields),
            'reason_code': self.reason_code,
            'validation_result': self.validation_result,
            'outcome': self.outcome,
        }


def is_collection_started(classification) -> bool:
    """Conservative phase-derived estimate of whether collection may have begun.

    A registered phase outside the observation/analysis window -- including
    ``unknown`` for unclassified failures -- is treated as "possibly started",
    so anything we cannot prove is refused.
    """
    return classification.phase not in PRE_COLLECTION_PHASES


class RepairPolicyChecker:
    """Decides whether a failure is eligible for repair. It never repairs."""

    def __init__(self, max_attempts: int = MAX_REPAIR_ATTEMPTS):
        if type(max_attempts) is not int or max_attempts < 0:
            raise ValueError('invalid_max_repair_attempts')
        self.max_attempts = max_attempts

    def check(self, error, context: RepairContext | None = None) -> RepairDecision:
        context = context if context is not None else RepairContext()
        classification = classify(error)
        if classification.category is Category.UNCLASSIFIED:
            return RepairDecision(False, RepairReason.UNCLASSIFIED)
        if not classification.repairable:
            return RepairDecision(False, RepairReason.NOT_REPAIRABLE)
        if context.attempts >= self.max_attempts:
            return RepairDecision(False, RepairReason.BUDGET_EXHAUSTED)
        if is_collection_started(classification):
            return RepairDecision(False, RepairReason.COLLECTION_STARTED)
        if not context.original_plan_hash:
            return RepairDecision(False, RepairReason.MISSING_PLAN)
        return RepairDecision(True, RepairReason.REPAIRABLE)


# --- deterministic candidate generation ---------------------------------
#
# 规则表是封闭的：只有显式注册的错误码才可能产出候选，其余一律 fail closed。每个
# 规则只依据「已观察证据 + 原计划」做机械变换，不调用模型、不发起请求、不改原计划。

STRUCTURAL_POINTERS = ('items_pointer', 'total_pointer', 'has_next_pointer')


def _error_code(error) -> str:
    code = getattr(error, 'code', None)
    return code if isinstance(code, str) else type(error).__name__


def _escape(token) -> str:
    return str(token).replace('~', '~0').replace('/', '~1')


def _response_members(body) -> list:
    """已观察响应体的直接成员（JSON Pointer → 值）；根是列表时只含根 ``''``。

    敏感键名按既有 SENSITIVE 契约跳过，绝不把敏感成员变成候选目标。
    """
    if isinstance(body, dict):
        return [('/' + _escape(key), body[key]) for key in sorted(body)[:MAX_CANDIDATE_MEMBERS]
                if not SENSITIVE.search(key)]
    if isinstance(body, list):
        return [('', body)]
    return []


def _failed_pointer(error):
    details = getattr(error, 'details', None)
    if isinstance(details, dict) and isinstance(details.get('pointer'), str):
        return details['pointer']
    return None


def _pointer_attribute(failed, plan):
    if failed is None:
        return None
    return next((name for name in STRUCTURAL_POINTERS if getattr(plan, name) == failed), None)


def _matches_kind(value, attribute) -> bool:
    if attribute == 'items_pointer':
        return isinstance(value, list) and bool(value) and isinstance(value[0], dict)
    if attribute == 'total_pointer':
        return type(value) is int and value >= 0
    return type(value) is bool


def _relocate_pointer(error, plan, record):
    attribute = _pointer_attribute(_failed_pointer(error), plan)
    if attribute is None:
        # 失败指针是字段内指针或未知指针：没有确定性迁移依据。
        return None
    candidates = sorted({path for path, value in _response_members(record.body)
                         if path != getattr(plan, attribute) and _matches_kind(value, attribute)})
    if len(candidates) != 1:
        # 0 个（无候选）或 >1 个（有歧义）都不确定，fail closed。
        return None
    return (plan.model_copy(update={attribute: candidates[0]}), (attribute,),
            ProposalReason.POINTER_RELOCATED)


def _page_location_from_evidence(error, plan, record):
    if record.method == 'GET':
        target = 'query'
        observed = record.query.get(plan.page_parameter)
        page_ok = isinstance(observed, str) and observed.isdecimal()
    else:
        target = 'json_body'
        body = record.request_body if isinstance(record.request_body, dict) else {}
        page_ok = type(body.get(plan.page_parameter)) is int
    if not page_ok or plan.pagination_location == target:
        return None
    return (plan.model_copy(update={'pagination_location': target}), ('pagination_location',),
            ProposalReason.PAGE_LOCATION_FROM_EVIDENCE)


def _no_deterministic_candidate(error, plan, record):
    # 页码成员在证据里不是整数：执行器只会发送整数页码，计划层没有安全的确定性修正，
    # 因此显式登记为「已识别但无候选」，而不是留下未定义行为。
    return None


DEFAULT_GENERATORS = {
    'pointer_not_found': _relocate_pointer,
    'page_location_mismatch': _page_location_from_evidence,
    'invalid_page_field_type': _no_deterministic_candidate,
}


def _validation_code(error) -> str:
    code = getattr(error, 'code', None)
    if isinstance(code, str):
        return code
    message = str(error)
    return message if re.fullmatch(r'[a-z][a-z_]{0,40}', message) else type(error).__name__


def _validate_candidate(candidate, record):
    """用执行器同一套 validate_plan 校验候选，绝不发起请求。"""
    try:
        validate_plan(candidate, record)
    except ValueError as error:
        return False, f'invalid:{_validation_code(error)}'
    return True, 'valid'


def _rejected_proposal(original_plan_hash) -> RepairProposal:
    return RepairProposal(original_plan_hash, None, (), ProposalReason.NO_DETERMINISTIC_CANDIDATE.value,
                          None, ProposalOutcome.REJECTED.value)


class RepairProposer:
    """Deterministic candidate generation; never re-executes, re-observes or calls a model."""

    def __init__(self, generators=None):
        self.generators = dict(DEFAULT_GENERATORS if generators is None else generators)

    def propose(self, error, plan, record) -> RepairProposal | None:
        if plan is None:
            return None
        original = plan_hash(plan)
        generator = self.generators.get(_error_code(error))
        if record is None or generator is None:
            return _rejected_proposal(original)
        generated = generator(error, plan, record)
        if generated is None:
            return _rejected_proposal(original)
        candidate, modified_fields, reason = generated
        candidate_hash = plan_hash(candidate)
        if candidate_hash == original:
            # 候选与原计划逐字节一致，不构成修复。
            return _rejected_proposal(original)
        valid, validation_result = _validate_candidate(candidate, record)
        return RepairProposal(original, candidate_hash, tuple(modified_fields), reason.value,
                              validation_result,
                              ProposalOutcome.PROPOSED.value if valid else ProposalOutcome.REJECTED.value)


def propose(error, plan, record) -> RepairProposal | None:
    """Convenience wrapper around the default deterministic proposer."""
    return RepairProposer().propose(error, plan, record)


def build_attempt(error, context: RepairContext | None = None, *, attempt: int | None = None,
                  checker: RepairPolicyChecker | None = None, decision: RepairDecision | None = None,
                  proposal: RepairProposal | None = None) -> RepairAttempt:
    """Audit one repair decision without executing any repair."""
    context = context if context is not None else RepairContext()
    classification = classify(error)
    if decision is None:
        decision = (checker or RepairPolicyChecker()).check(error, context)
    number = context.attempts + 1 if attempt is None else attempt
    return RepairAttempt(
        attempt=number,
        error_code=classification.code,
        error_category=classification.category.value,
        failure_phase=classification.phase,
        collection_started=is_collection_started(classification),
        original_plan_hash=context.original_plan_hash,
        candidate_plan_hash=proposal.candidate_plan_hash if proposal is not None else None,
        modified_fields=proposal.modified_fields if proposal is not None else (),
        reason_code=decision.reason.value if decision.eligible else None,
        validation_result=proposal.validation_result if proposal is not None else None,
        rejection_code=None if decision.eligible else decision.reason.value,
        outcome=RepairOutcome.DEFERRED.value if decision.eligible else RepairOutcome.REJECTED.value,
    )


def audit_failure(error, context: RepairContext | None = None, plan=None, record=None, *,
                  checker: RepairPolicyChecker | None = None,
                  proposer: RepairProposer | None = None):
    """Full audit of one failure: eligibility, deterministic proposal, attempt.

    Proposals are only ever generated for eligible failures, so a forbidden
    category can never yield a candidate. Returns ``(attempt, proposal)`` where
    ``proposal`` is ``None`` when nothing was proposed.
    """
    context = context if context is not None else RepairContext()
    checker = checker or RepairPolicyChecker()
    decision = checker.check(error, context)
    proposal = None
    if decision.eligible and plan is not None:
        proposal = (proposer or RepairProposer()).propose(error, plan, record)
    attempt = build_attempt(error, context, checker=checker, decision=decision, proposal=proposal)
    return attempt, proposal


def audit_document(attempts, proposals=()) -> dict:
    """Serialize repair attempts and proposals as the ``repair.json`` payload."""
    return {'schema_version': 1,
            'attempts': [attempt.to_dict() for attempt in attempts],
            'proposals': [proposal.to_dict() for proposal in proposals]}
