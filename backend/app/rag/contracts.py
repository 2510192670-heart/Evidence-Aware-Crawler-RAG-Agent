"""Retrieval output contract and the M5.1 / M5.2 additive interfaces.

This module declares shapes and constants only: it does not extract features,
rank cases or read case data. M5.1-B appends feature keys; M5.2-B appends
failure-aware knowledge keys -- but only when the corpus carries structured
knowledge (``failure_modes``). A disabled result still carries exactly
:data:`LEGACY_RESULT_KEYS`.
"""

from typing import TypedDict

# The only ranking algorithm allowed by the frozen M4.4 retrieval contract.
ALGORITHM = 'BM25Okapi'

# Keys every retrieval result has carried since before M5.0. Later milestones may
# add keys, but must never rename or remove these.
LEGACY_RESULT_KEYS = ('enabled', 'algorithm', 'corpus_version', 'corpus_sha256', 'cases')

# Additive keys M5.1-B appends to an enabled result. A disabled result carries
# only :data:`LEGACY_RESULT_KEYS`, keeping the RAG-off output byte-compatible.
FEATURE_RESULT_KEYS = ('feature_schema_version', 'query_features', 'gate')

# Additive keys M5.2-B appends, and only when the corpus has structured knowledge,
# so a version 1 corpus keeps the exact M5.1 result.
KNOWLEDGE_RESULT_KEYS = ('case_schema_version', 'knowledge_gate')

# Version of the evidence-feature schema.
FEATURE_SCHEMA_VERSION = 1


class RetrievalCase(TypedDict):
    """One curated case as returned in ``cases`` (legacy fields plus score)."""

    id: str
    title: str
    terms: list[str]
    guidance: str
    score: float


class QueryFeatures(TypedDict, total=False):
    """Evidence features; closed vocabulary, never response values."""

    method: str
    page_location: str
    items_container: str
    stop_signal: str
    continuation_flag: str
    field_mapping: str
    ambiguity: str


class FeatureGate(TypedDict):
    """Deterministic audit of how evidence features affected the ranking."""

    applied: bool
    filtered: list[str]
    matched: list[str]
    conflicted: list[str]


class RetrievalResult(TypedDict):
    """The shape of ``retrieval.json``.

    The legacy keys are always present; the M5.1 feature keys are appended for an
    enabled result; the M5.2 knowledge keys are appended only when the corpus
    carries structured knowledge.
    """

    enabled: bool
    algorithm: str
    corpus_version: int
    corpus_sha256: str
    cases: list[RetrievalCase]
    feature_schema_version: int
    query_features: QueryFeatures
    gate: FeatureGate
    case_schema_version: int
    knowledge_gate: FeatureGate
