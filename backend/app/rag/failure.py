"""M5.3-A: runtime failure context for diagnostic knowledge retrieval.

Projects an ``errors.classify()`` result into the ``failure_context`` consumed by
``retrieval.retrieve(..., failure_context=...)`` and builds the
``failure_retrieval.json`` schema.

This module is read-only and side-effect free. It never calls a model, never
touches execution and never triggers a repair: it only decides *what the observed
failure was* so retrieval can look up matching knowledge. The taxonomy in
``pipeline/errors.py`` stays the single source of truth -- callers pass the
exception only, so they cannot override the category or phase -- and
``repair.py`` remains the sole authority on repairability.
"""

from ..pipeline.errors import Category, classify

FAILURE_SCHEMA_VERSION = 1

# 生命周期归类。executing 同时覆盖执行与验证阶段：验证失败与执行失败共用该 phase，
# 仅凭 registry 无法区分，因此统一记为 execution，不臆造第五个阶段。
UNKNOWN_FAILURE_PHASE = 'unknown'
FAILURE_PHASES = ('observation', 'planning', 'execution', 'unknown')
_PHASE_TO_FAILURE_PHASE = {'observing': 'observation', 'analyzing': 'planning',
                           'executing': 'execution'}


def failure_phase(classification):
    """Map a classification phase onto the coarse lifecycle bucket."""
    return _PHASE_TO_FAILURE_PHASE.get(classification.phase, UNKNOWN_FAILURE_PHASE)


def _context(classification):
    """Project a classification onto the failure_context, registry values only."""
    return {'error_code': classification.code, 'category': classification.category.value,
            'phase': classification.phase}


def build_failure_context(error):
    """Return the failure context for ``error``, or ``None`` when it is unusable.

    Only ``errors.classify()`` decides the values, so a caller cannot override
    the category or phase. An ``UNCLASSIFIED`` failure is refused (fail closed):
    without a trustworthy taxonomy entry there is no context to retrieve with.
    """
    classification = classify(error)
    return None if classification.category is Category.UNCLASSIFIED else _context(classification)


def build_failure_retrieval(error, retrieval_result):
    """Build the ``failure_retrieval.json`` document, or ``None`` if none applies.

    Carries only closed-vocabulary codes, curated case ids and hashes -- never a
    response value, query value, URL or sensitive field. When the corpus has no
    structured knowledge the retrieval result has no ``knowledge_gate``; the
    document then reports an explicit inactive gate instead of inventing one.
    """
    classification = classify(error)
    if classification.category is Category.UNCLASSIFIED or not isinstance(retrieval_result, dict):
        return None
    gate = retrieval_result.get('knowledge_gate')
    if not isinstance(gate, dict):
        gate = {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}
    return {
        'schema_version': FAILURE_SCHEMA_VERSION,
        'failure_context': _context(classification),
        'failure_phase': failure_phase(classification),
        'knowledge_gate': {'applied': bool(gate.get('applied')),
                           'filtered': list(gate.get('filtered', [])),
                           'matched': list(gate.get('matched', [])),
                           'conflicted': list(gate.get('conflicted', []))},
        'case_ids': [case['id'] for case in retrieval_result.get('cases', [])
                     if isinstance(case, dict) and isinstance(case.get('id'), str)],
        'corpus_sha256': retrieval_result.get('corpus_sha256'),
    }
