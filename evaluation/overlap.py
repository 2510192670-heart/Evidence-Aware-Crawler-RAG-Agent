"""M6.1-B2 deterministic semantic overlap detector (evaluation-only).

Detects benchmark contamination / semantic overlap between development and
evaluation splits. The detector REPORTS EVIDENCE; it never proves independence.
``AUTO_PASS`` means "no deterministic overlap signal fired", NOT "semantic
independence proven".

Hard guarantees: deterministic, offline, explainable, fail-closed. There is no
LLM, no embedding, no semantic model, no similarity percentage, no weighted score
and no threshold classifier. The decision is a fixed three-state rule::

    if strong_signals:   BLOCK
    elif weak_signals:   MANUAL_REVIEW
    else:                AUTO_PASS

Weak signals never escalate: any number of weak signals without a strong signal
is still ``MANUAL_REVIEW``. There is no ``weak_count >= N`` and no ``score >= x``.

Boundary: this module validates/computes SHAPE and evidence only. The core
(``normalize_case`` / ``compare`` / ``audit_cross_split``) is a pure, IO-free
function set importing nothing but ``evaluation.schemas.hashing`` and the standard
library, so canonicalization authority stays with the existing ``digest`` helper
(never Python's salted builtin ``hash()``, never a second canonical JSON
algorithm). The only IO is ``load_m6_v1_views``, a separate projection helper that
reads the already-frozen m6-v1 bundle; integrity of that bundle is the authority of
``contract.validate_bundle`` / ``contract_v2.assert_m61a_frozen``, run elsewhere.

Results are leak-safe: ``OverlapResult`` carries only case ids, the decision,
sorted signal ids and evidence made of hashes / counts / relations. Raw oracle
values, records, response bodies, secrets and canary plaintext never enter a
result, so ``json.dumps(result.to_jsonable())`` cannot leak source data.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from evaluation.schemas.hashing import digest

__all__ = ['NormalizedView', 'OverlapDecision', 'OverlapResult', 'audit_cross_split',
           'compare', 'load_m6_v1_views', 'normalize_case']

# ---------------------------------------------------------------------------
# Signal codes. Stable, human-readable ids; the id IS the explanation.
# Strong (any -> BLOCK) and weak (any, no strong -> MANUAL_REVIEW) are disjoint.
# ---------------------------------------------------------------------------
S1_SAME_FAMILY_ID = 'S1_same_family_id'
S2_EXACT_STRUCTURAL_SIGNATURE = 'S2_exact_structural_signature'
S3_EXACT_ORACLE_RESULT_HASH = 'S3_exact_oracle_result_hash'
S4_EXACT_RECORD_SET = 'S4_exact_record_set'
S5_EXACT_RESPONSE_PAYLOAD = 'S5_exact_response_payload'
W1_PARTIAL_RECORD_OVERLAP = 'W1_partial_record_overlap'
W2_NESTED_RESPONSE_SUBTREE_OVERLAP = 'W2_nested_response_subtree_overlap'
W3_ENDPOINT_COMPOSITION_MATCH = 'W3_endpoint_composition_match'
W4_PAGINATION_SEMANTICS_MATCH = 'W4_pagination_semantics_match'
W5_FAILURE_CODE_MATCH = 'W5_failure_code_match'
W6_REPAIR_DISPOSITION_MATCH = 'W6_repair_disposition_match'

STRONG_SIGNALS = (S1_SAME_FAMILY_ID, S2_EXACT_STRUCTURAL_SIGNATURE, S3_EXACT_ORACLE_RESULT_HASH,
                  S4_EXACT_RECORD_SET, S5_EXACT_RESPONSE_PAYLOAD)
WEAK_SIGNALS = (W1_PARTIAL_RECORD_OVERLAP, W2_NESTED_RESPONSE_SUBTREE_OVERLAP,
                W3_ENDPOINT_COMPOSITION_MATCH, W4_PAGINATION_SEMANTICS_MATCH,
                W5_FAILURE_CODE_MATCH, W6_REPAIR_DISPOSITION_MATCH)

# Request keys that carry a page cursor. Used only to locate pagination, never to
# read a literal value into a signature.
_PAGE_KEYS = frozenset({'cursor', 'offset', 'page', 'page_number', 'page_token'})


class OverlapDecision(str, Enum):
    """Three-state verdict. No confidence, no numeric similarity, no threshold."""

    AUTO_PASS = 'auto_pass'
    MANUAL_REVIEW = 'manual_review'
    BLOCK = 'block'


@dataclass(frozen=True)
class NormalizedView:
    """Evaluation-only normalized projection of one case.

    Carries digests and closed structural descriptors only -- never raw oracle
    records, response bodies or secrets. Built from V1 raw documents today; the
    same field set also fits V2 (no V1 migration and no forced ``exposure``).
    """

    case_id: str
    split: str
    family_id: str
    family_signature_digest: str
    oracle_result_sha256: str | None
    record_hashes: frozenset
    response_payload_hashes: frozenset
    nested_subtree_hashes: frozenset
    endpoint_composition_signature: str
    pagination_signature: str | None
    failure_code: str | None
    repair_disposition: str


@dataclass(frozen=True)
class OverlapResult:
    """Immutable, leak-safe verdict for one canonical cross-split pair."""

    left_case_id: str
    right_case_id: str
    decision: OverlapDecision
    strong_signals: tuple
    weak_signals: tuple
    evidence: Mapping = field(hash=False)

    def to_jsonable(self):
        """Plain JSON form: case ids, decision value, sorted signal ids, evidence
        (relations / counts / digests only). No raw source data can appear here."""
        return {'left_case_id': self.left_case_id, 'right_case_id': self.right_case_id,
                'decision': self.decision.value, 'strong_signals': list(self.strong_signals),
                'weak_signals': list(self.weak_signals),
                'evidence': {k: dict(self.evidence[k]) for k in sorted(self.evidence)}}


# ---------------------------------------------------------------------------
# Pure structural projections. These read shapes and type names only; literal
# values, hosts, ports, secrets and oracle answers never enter a signature.
# ---------------------------------------------------------------------------
def _container_kind(node):
    """Coarse response-container shape: root kind, whether an array (items) child
    exists, and the set of child value type NAMES. No literal values, no keys."""
    if isinstance(node, dict):
        return {'root': 'object', 'has_array_child': any(isinstance(v, list) for v in node.values()),
                'child_kinds': sorted({type(v).__name__ for v in node.values()})}
    if isinstance(node, list):
        return {'root': 'array', 'has_array_child': False,
                'child_kinds': sorted({type(v).__name__ for v in node})}
    return {'root': 'null' if node is None else type(node).__name__, 'has_array_child': False,
            'child_kinds': []}


def _page_kind_and_location(query, body):
    """Return (page_kind, page_location) from request key NAMES only."""
    query_keys = set(query or {})
    body_keys = set(body or {})
    if 'cursor' in query_keys or 'cursor' in body_keys:
        kind = 'cursor'
    elif 'offset' in query_keys or 'offset' in body_keys:
        kind = 'offset'
    else:
        kind = 'page_number'
    if query_keys & _PAGE_KEYS:
        location = 'query'
    elif body_keys & _PAGE_KEYS:
        location = 'json_body'
    else:
        location = 'none'
    return kind, location


def _continuation_flags(responses):
    """Sorted top-level boolean key NAMES across responses (e.g. ['more']). Keys are
    structural pagination markers, not values; the result is digested, never stored raw."""
    flags = set()
    for body in responses:
        if isinstance(body, dict):
            flags.update(k for k, v in body.items() if isinstance(v, bool))
    return sorted(flags)


def _stop_signal(responses):
    for body in responses:
        if isinstance(body, dict):
            total = body.get('total')
            if isinstance(total, int) and not isinstance(total, bool):
                return 'total_present'
    return 'flag_only' if _continuation_flags(responses) else 'none'


def _endpoint_composition(fixture):
    """Weak composition descriptor (W3): endpoint count plus per-endpoint method,
    request placement, pagination location and coarse response container shape.
    Deliberately weaker than the authoritative family signature (S2): it omits the
    full body shape, host, port, literal values, secrets and the oracle answer."""
    endpoints = []
    for endpoint in fixture['endpoints']:
        query = endpoint.get('query') or {}
        body = endpoint.get('request_body') or {}
        responses = endpoint.get('responses') or []
        _, location = _page_kind_and_location(query, body)
        endpoints.append({'method': endpoint['method'], 'query_placement': bool(query),
                          'body_placement': bool(body), 'pagination_location': location,
                          'response_container': _container_kind(responses[0] if responses else None)})
    endpoints.sort(key=digest)
    return {'endpoint_count': len(endpoints), 'endpoints': endpoints}


def _pagination_signature(fixture):
    """Weak pagination descriptor (W4). Returns a digest, or None when no endpoint
    paginates. This is NEVER folded into the S2 structural signature: pagination
    semantics alone can only ever be a weak signal."""
    descriptors = []
    for endpoint in fixture['endpoints']:
        query = endpoint.get('query') or {}
        body = endpoint.get('request_body') or {}
        kind, location = _page_kind_and_location(query, body)
        if location == 'none':
            continue
        responses = endpoint.get('responses') or []
        descriptors.append({'method': endpoint['method'], 'page_location': location, 'page_kind': kind,
                            'continuation_flag': _continuation_flags(responses),
                            'stop_signal': _stop_signal(responses)})
    if not descriptors:
        return None
    descriptors.sort(key=digest)
    return digest({'pagination': descriptors})


def _collect_subtrees(payload):
    """Canonical digests of every non-empty PROPER nested container (dict/list) inside
    one response payload. Excludes the root payload itself (that is S5 territory),
    empty {} / [], and scalars. Iterative so deep nesting cannot recurse."""
    subtrees = set()
    stack = [(payload, True)]
    while stack:
        node, is_root = stack.pop()
        if isinstance(node, dict):
            if not is_root and node:
                subtrees.add(digest(node))
            stack.extend((child, False) for child in node.values() if isinstance(child, (dict, list)))
        elif isinstance(node, list):
            if not is_root and node:
                subtrees.add(digest(node))
            stack.extend((child, False) for child in node if isinstance(child, (dict, list)))
    return subtrees


# ---------------------------------------------------------------------------
# Pure projection: raw V1/V2 documents -> NormalizedView. Fail-closed.
# ---------------------------------------------------------------------------
def normalize_case(case_raw, oracle_raw, fixture_raw, family_signature):
    """Project one case (raw dicts already loaded) into a NormalizedView.

    Reads only: case id/split/family_id/repair disposition, oracle result hash /
    records / expected error code, and the fixture endpoint response sequence. It
    never reads ``exposure`` and never requires a V1 migration.
    """
    if not isinstance(case_raw, dict) or not isinstance(oracle_raw, dict) \
            or not isinstance(fixture_raw, dict):
        raise ValueError('invalid_projection_input')
    case_id, split, family_id = case_raw.get('id'), case_raw.get('split'), case_raw.get('family_id')
    if not isinstance(case_id, str) or not case_id:
        raise ValueError('missing_case_id')
    if not isinstance(split, str) or not split:
        raise ValueError('missing_split')
    if not isinstance(family_id, str) or not family_id:
        raise ValueError('missing_family_id')
    repair = case_raw.get('repair_expectation')
    disposition = repair.get('policy_disposition') if isinstance(repair, dict) else None
    if not isinstance(disposition, str) or not disposition:
        raise ValueError('missing_repair_disposition')

    endpoints = fixture_raw.get('endpoints')
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError('missing_endpoints')
    payload_hashes, subtree_hashes = set(), set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get('method'), str):
            raise ValueError('invalid_endpoint')
        responses = endpoint.get('responses')
        if not isinstance(responses, list) or not responses:
            raise ValueError('invalid_response_sequence')
        for body in responses:
            payload_hashes.add(digest(body))
            subtree_hashes |= _collect_subtrees(body)

    records = oracle_raw.get('records')
    if records is None:
        records = []
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError('invalid_records')
    oracle_sha = oracle_raw.get('result_sha256')
    if oracle_sha is not None and not isinstance(oracle_sha, str):
        raise ValueError('invalid_oracle_result_sha256')
    failure_code = oracle_raw.get('expected_error_code')
    if failure_code is not None and not isinstance(failure_code, str):
        raise ValueError('invalid_failure_code')

    return NormalizedView(
        case_id=case_id, split=split, family_id=family_id,
        family_signature_digest=digest(family_signature), oracle_result_sha256=oracle_sha,
        record_hashes=frozenset(digest(record) for record in records),
        response_payload_hashes=frozenset(payload_hashes),
        nested_subtree_hashes=frozenset(subtree_hashes),
        endpoint_composition_signature=digest(_endpoint_composition(fixture_raw)),
        pagination_signature=_pagination_signature(fixture_raw),
        failure_code=failure_code, repair_disposition=disposition)


# ---------------------------------------------------------------------------
# Pure decision core.
# ---------------------------------------------------------------------------
def _decide(strong, weak):
    """Fixed rule. No score, no threshold, no weak-count escalation."""
    if strong:
        return OverlapDecision.BLOCK
    if weak:
        return OverlapDecision.MANUAL_REVIEW
    return OverlapDecision.AUTO_PASS


def compare(left, right):
    """Compare two views into one canonical result. ``compare(A, B)`` and
    ``compare(B, A)`` are identical: the pair is ordered by case_id and every signal
    is symmetric. Pure and IO-free."""
    if not isinstance(left, NormalizedView) or not isinstance(right, NormalizedView):
        raise ValueError('invalid_view')
    if right.case_id < left.case_id:
        left, right = right, left
    if left.case_id == right.case_id:
        raise ValueError('compare_requires_distinct_case_ids')

    strong, weak, evidence = [], [], {}

    def record(signal, bucket, **fields):
        bucket.append(signal)
        evidence[signal] = fields

    # --- Strong signals (any -> BLOCK) ---
    if left.family_id == right.family_id:
        record(S1_SAME_FAMILY_ID, strong, relation='family_id_equal')
    if left.family_signature_digest == right.family_signature_digest:
        record(S2_EXACT_STRUCTURAL_SIGNATURE, strong, relation='family_signature_equal',
               digest=left.family_signature_digest)
    if left.oracle_result_sha256 is not None and left.oracle_result_sha256 == right.oracle_result_sha256:
        record(S3_EXACT_ORACLE_RESULT_HASH, strong, relation='oracle_result_hash_equal',
               digest=left.oracle_result_sha256)
    left_records, right_records = left.record_hashes, right.record_hashes
    exact_record_set = bool(left_records) and left_records == right_records
    if exact_record_set:
        record(S4_EXACT_RECORD_SET, strong, relation='record_set_equal', count=len(left_records),
               digest=digest(sorted(left_records)))
    shared_payloads = left.response_payload_hashes & right.response_payload_hashes
    if shared_payloads:
        record(S5_EXACT_RESPONSE_PAYLOAD, strong, relation='response_payload_shared',
               count=len(shared_payloads), digest=digest(sorted(shared_payloads)))

    # --- Weak signals (any, no strong -> MANUAL_REVIEW) ---
    if not exact_record_set:
        shared_records = left_records & right_records
        if shared_records:
            record(W1_PARTIAL_RECORD_OVERLAP, weak, relation='record_partial_overlap',
                   shared_count=len(shared_records), left_count=len(left_records),
                   right_count=len(right_records))
    shared_subtrees = left.nested_subtree_hashes & right.nested_subtree_hashes
    if shared_subtrees:
        record(W2_NESTED_RESPONSE_SUBTREE_OVERLAP, weak, relation='nested_subtree_shared',
               count=len(shared_subtrees), digest=digest(sorted(shared_subtrees)))
    if left.endpoint_composition_signature == right.endpoint_composition_signature:
        record(W3_ENDPOINT_COMPOSITION_MATCH, weak, relation='endpoint_composition_equal',
               digest=left.endpoint_composition_signature)
    if left.pagination_signature is not None and left.pagination_signature == right.pagination_signature:
        record(W4_PAGINATION_SEMANTICS_MATCH, weak, relation='pagination_semantics_equal',
               digest=left.pagination_signature)
    if left.failure_code is not None and left.failure_code == right.failure_code:
        record(W5_FAILURE_CODE_MATCH, weak, relation='failure_code_equal')
    if left.repair_disposition != 'none' and left.repair_disposition == right.repair_disposition:
        record(W6_REPAIR_DISPOSITION_MATCH, weak, relation='repair_disposition_equal')

    return OverlapResult(left.case_id, right.case_id, _decide(strong, weak), tuple(sorted(strong)),
                         tuple(sorted(weak)), MappingProxyType(evidence))


def audit_cross_split(views):
    """Audit every cross-split pair. Only different splits are compared. Output is
    sorted by (left_case_id, right_case_id), so input order never changes it.
    Fail-closed on an empty or duplicated case_id."""
    views = list(views)
    ids = [view.case_id for view in views]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids):
        raise ValueError('empty_case_id')
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate_case_id')
    results = [compare(views[i], views[j]) for i in range(len(views)) for j in range(i + 1, len(views))
               if views[i].split != views[j].split]
    results.sort(key=lambda result: (result.left_case_id, result.right_case_id))
    return results


# ---------------------------------------------------------------------------
# IO boundary (the only file access in this module). Reads the frozen m6-v1
# bundle and projects each case. Bundle integrity is validated elsewhere
# (contract.validate_bundle / contract_v2.assert_m61a_frozen); this helper only
# reads JSON under evaluation/ and fails closed on an escaping or missing path.
# ---------------------------------------------------------------------------
def load_m6_v1_views(root, base='evaluation/datasets/m6-v1/'):
    root = Path(root)
    evaluation_root = (root / 'evaluation').resolve()

    def read(ref):
        if not isinstance(ref, str) or not ref.startswith('evaluation/'):
            raise ValueError('invalid_relative_reference')
        path = (root / ref).resolve()
        if not path.is_relative_to(evaluation_root) or not path.is_file():
            raise ValueError('missing_or_escaping_reference')
        return json.loads(path.read_text(encoding='utf-8'))

    manifest = read(base + 'manifest.json')
    families_ref = manifest.get('families')
    cases_ref = manifest.get('cases')
    if not isinstance(families_ref, str) or not isinstance(cases_ref, list):
        raise ValueError('invalid_manifest')
    families = {family['id']: family for family in read(families_ref)}
    views = []
    for case_ref in cases_ref:
        case_raw = read(case_ref)
        family = families.get(case_raw.get('family_id'))
        if family is None:
            raise ValueError('unknown_family')
        views.append(normalize_case(case_raw, read(case_raw['oracle']['ref']),
                                    read(case_raw['fixture']['ref']), family['signature']))
    return views
