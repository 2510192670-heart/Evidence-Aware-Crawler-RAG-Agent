"""BM25 over structural keys; no model call, embeddings, or response values.

Ranking stays text-first and deterministic:

    BM25  ->  feature gate (M5.1)  ->  failure-aware stage (M5.2)

M5.1-B compares evidence-derived query features against the optional
``features`` a curated case may declare. M5.2-B adds a failure-aware stage: when
the corpus carries structured knowledge (``knowledge.load``), the observed
failure context is matched against a case's declared failure modes. Both layers
only reject safety-boundary conflicts and lightly adjust the score; BM25 remains
the ranking core.

A case without ``features`` or ``failure_modes`` (every case in the frozen
corpus) is unaffected, so legacy BM25 behaviour and the M5.1 result contract are
preserved exactly.
"""

import hashlib
import json
from pathlib import Path
import re

from rank_bm25 import BM25Okapi

from ..pipeline.errors import SPECS, Category
from .contracts import ALGORITHM, FEATURE_SCHEMA_VERSION

# 单特征匹配裁决，闭集枚举，绝不生成字符串。
MATCH = 'match'
CONFLICT = 'conflict'
IGNORED = 'ignored'
BOUNDARY = 'boundary'

# method 是 M4.3 证据驱动执行的硬边界（GET/POST 不可互转）。一个声明了相反
# method 的 case 可能诱导规划器转换请求语义，因此直接过滤，而不是降权。
BOUNDARY_FEATURES = ('method',)

# 轻量排序增强：精确匹配的奖励略高于冲突惩罚。分值远小于典型 BM25 差距，
# 使 feature 只作为结构匹配的微调，不取代文本相关性。
MATCH_BONUS = 0.5
CONFLICT_PENALTY = 0.25

# M5.2: failure-aware 阶段的轻量权重，同样只做微调。类别/阶段词汇全部取自
# errors.py 的 registry 与 Category，不手写第二套 taxonomy。
FAILURE_MATCH_BONUS = 0.5
FAILURE_CONFLICT_PENALTY = 0.25
_CATEGORY_VALUES = frozenset(category.value for category in Category)
# 受限类别：这些失败一律禁止推荐带有 auto_applied 可修复性知识的案例。
RESTRICTED_CATEGORIES = frozenset({Category.SECURITY.value, Category.DATA_INTEGRITY.value,
                                   Category.TRANSPORT.value, Category.UNCLASSIFIED.value})


def tokens(value):
    return re.findall(r'[a-z0-9_]+', value.lower())


def structure_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from tokens(key)
            yield from structure_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from structure_keys(child)


def match_features(query_features, case_features):
    """Deterministically compare a query's features against a case's features.

    Closed-vocabulary enum comparison only: no fuzzy reasoning, no model call
    and no string generation. ``unknown`` (or a feature absent) on either side
    is neutral. An exact enum match earns a small bonus; any other concrete
    disagreement is a conflict and downweights the case. A conflict on a
    safety-boundary feature rejects the case outright so the planner is never
    nudged to convert GET and POST.
    """
    # 延迟导入：features 依赖本模块的 tokens()，顶层导入会形成循环。
    from .features import FEATURE_KEYS, UNKNOWN

    query_features = query_features if isinstance(query_features, dict) else {}
    case_features = case_features if isinstance(case_features, dict) else {}
    verdicts = {}
    matched = conflicted = 0
    filtered = False
    for key in FEATURE_KEYS:
        query_value = query_features.get(key, UNKNOWN)
        case_value = case_features.get(key)
        if case_value is None or case_value == UNKNOWN or query_value == UNKNOWN:
            verdicts[key] = IGNORED
        elif query_value == case_value:
            verdicts[key] = MATCH
            matched += 1
        elif key in BOUNDARY_FEATURES:
            verdicts[key] = BOUNDARY
            filtered = True
        else:
            verdicts[key] = CONFLICT
            conflicted += 1
    return {'verdicts': verdicts, 'matched': matched, 'conflicted': conflicted,
            'filtered': filtered,
            'adjustment': round(matched * MATCH_BONUS - conflicted * CONFLICT_PENALTY, 6)}


def _failure_context(value):
    """Normalize an observed failure from ``errors.SPECS`` (single source of truth).

    A registered code is authoritative: its category and phase come from the
    registry, never from the caller. An unregistered code stays ``known=False``
    (neutral), and its category is only accepted if it is a real
    :class:`errors.Category` value -- so a hand-written taxonomy cannot enter.
    """
    if not isinstance(value, dict):
        return None
    code = value.get('error_code')
    spec = SPECS.get(code) if isinstance(code, str) else None
    if spec is not None:
        return {'error_code': code, 'category': spec.category.value, 'phase': spec.phase, 'known': True}
    category = value.get('category')
    if category not in _CATEGORY_VALUES:
        category = None
    phase = value.get('phase')
    return {'error_code': code if isinstance(code, str) else None, 'category': category,
            'phase': phase if isinstance(phase, str) else None, 'known': False}


def failure_match(query_context, knowledge_case):
    """Deterministically compare an observed failure against a case's knowledge.

    ``query_context`` carries the evidence ``query_features`` and an optional
    ``failure_context`` (``error_code`` + optional ``category``/``phase``).
    ``knowledge_case`` is a validated ``knowledge.KnowledgeCase`` (or ``None``
    for a legacy case); the failure knowledge itself is never read from raw JSON.

    Reuses the M5.1 verdict vocabulary. Rules:

    1. exact ``error_code`` match earns a bonus,
    2. a ``category`` mismatch downweights,
    3. a ``phase`` mismatch downweights,
    4. a restricted failure (SECURITY / DATA_INTEGRITY / TRANSPORT /
       UNCLASSIFIED) rejects any case that carries ``auto_applied`` knowledge
       (a safety boundary),
    5. an unknown code is neutral.

    Legacy cases (no ``failure_modes``) and a missing failure context are
    entirely neutral, so the stage is inert for a version 1 corpus.
    """
    # 延迟导入：knowledge -> features -> retrieval 形成循环，顶层导入会失败。
    from .knowledge import AUTO_APPLIED, KnowledgeCase

    query_context = query_context if isinstance(query_context, dict) else {}
    failure = _failure_context(query_context.get('failure_context'))
    verdicts = {'error_code': IGNORED, 'category': IGNORED, 'phase': IGNORED, 'repairability': IGNORED}
    matched = conflicted = 0
    filtered = False
    if isinstance(knowledge_case, KnowledgeCase):
        has_auto_applied = any(disposition == AUTO_APPLIED
                               for disposition in knowledge_case.repairability.values())
        # 规则 4：受限类别失败不得推荐带有 auto_applied 知识的案例。
        if failure is not None and failure['category'] in RESTRICTED_CATEGORIES and has_auto_applied:
            verdicts['repairability'] = BOUNDARY
            filtered = True
        if failure is not None and failure['known']:
            codes = {mode.code for mode in knowledge_case.failure_modes}
            categories = {mode.category for mode in knowledge_case.failure_modes}
            phases = {mode.phase for mode in knowledge_case.failure_modes}
            if failure['error_code'] in codes:
                verdicts['error_code'] = MATCH
                matched += 1
            if categories:
                if failure['category'] in categories:
                    verdicts['category'] = MATCH
                    matched += 1
                else:
                    verdicts['category'] = CONFLICT
                    conflicted += 1
            if phases:
                if failure['phase'] in phases:
                    verdicts['phase'] = MATCH
                    matched += 1
                else:
                    verdicts['phase'] = CONFLICT
                    conflicted += 1
    return {'verdicts': verdicts, 'matched': matched, 'conflicted': conflicted, 'filtered': filtered,
            'adjustment': round(matched * FAILURE_MATCH_BONUS - conflicted * FAILURE_CONFLICT_PENALTY, 6)}


def _knowledge_view(cases):
    # 延迟导入：knowledge 依赖 features，features 依赖本模块的 tokens()。
    from .knowledge import load
    return load(cases)


def _query_features(summaries, fields):
    # 延迟导入：features -> retrieval 存在循环依赖，顶层导入会失败。
    from .features import evidence_features
    return evidence_features(summaries, fields or [])


def retrieve(summaries, enabled=True, fields=None, cases=None, failure_context=None):
    """BM25 rank curated cases, then gate and lightly re-rank deterministically.

    ``fields`` are the requested output names (used only for the feature layer);
    ``cases`` is an optional corpus override for deterministic tests;
    ``failure_context`` is an optional observed failure from ``errors.SPECS``
    enabling the M5.2 failure-aware stage. All are additive: the running pipeline
    calls ``retrieve(summaries, enabled=...)``.

    The failure-aware keys are appended only when the corpus actually carries
    structured knowledge, so a version 1 corpus yields the M5.1 result unchanged.
    """
    if cases is None:
        raw = Path(__file__).with_name('cases.json').read_bytes()
        cases = json.loads(raw)
    else:
        # 注入语料时按其规范序列计算哈希，保证同一输入得到同一结果。
        raw = json.dumps(cases, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    result = {'enabled': enabled, 'algorithm': ALGORITHM, 'corpus_version': 1,
              'corpus_sha256': hashlib.sha256(raw).hexdigest(), 'cases': []}
    # RAG 关闭时输出与 M4.4 冻结结果逐字节一致：不追加任何特征字段。
    if not enabled:
        return result
    query = sorted(set(token for summary in summaries for section in ('query', 'response_shape')
                       for token in structure_keys(summary.get(section, {}))))
    corpus = [tokens(' '.join(case['terms'])) for case in cases]
    scores = BM25Okapi(corpus).get_scores(query)
    query_features = _query_features(summaries, fields)
    gate = {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}
    # 结构化知识只经 knowledge.load() 读取，绝不在此直接解析 JSON。
    knowledge = _knowledge_view(cases)
    knowledge_by_id = {case.id: case for case in knowledge}
    # 语料没有 failure_modes 时 failure-aware 阶段停用，输出与 M5.1 完全一致。
    knowledge_active = any(case.failure_modes for case in knowledge)
    knowledge_gate = {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}
    ranked = sorted(range(len(cases)), key=lambda i: (-float(scores[i]), cases[i]['id']))
    candidates = []
    for i in ranked:
        if scores[i] <= 0 or not set(query).intersection(corpus[i]):
            continue
        case = cases[i]
        match = match_features(query_features, case.get('features'))
        if match['filtered']:
            gate['filtered'].append(case['id'])
            gate['applied'] = True
            continue
        if match['matched']:
            gate['matched'].append(case['id'])
            gate['applied'] = True
        if match['conflicted']:
            gate['conflicted'].append(case['id'])
            gate['applied'] = True
        score = float(scores[i]) + match['adjustment']
        if knowledge_active:
            failure = failure_match({'query_features': query_features,
                                     'failure_context': failure_context},
                                    knowledge_by_id.get(case['id']))
            if failure['filtered']:
                knowledge_gate['filtered'].append(case['id'])
                knowledge_gate['applied'] = True
                continue
            if failure['matched']:
                knowledge_gate['matched'].append(case['id'])
                knowledge_gate['applied'] = True
            if failure['conflicted']:
                knowledge_gate['conflicted'].append(case['id'])
                knowledge_gate['applied'] = True
            score += failure['adjustment']
        candidates.append((i, score))
    candidates.sort(key=lambda item: (-item[1], cases[item[0]]['id']))
    for i, score in candidates[:2]:
        case = cases[i]
        # 结果条目始终保持 legacy 形状，即使 case 声明了 features 或 failure_modes。
        result['cases'].append({'id': case['id'], 'title': case['title'], 'terms': case['terms'],
                                'guidance': case['guidance'], 'score': round(score, 6)})
    result['feature_schema_version'] = FEATURE_SCHEMA_VERSION
    result['query_features'] = query_features
    result['gate'] = gate
    if knowledge_active:
        result['case_schema_version'] = max(case.case_schema_version for case in knowledge)
        result['knowledge_gate'] = knowledge_gate
    return result
