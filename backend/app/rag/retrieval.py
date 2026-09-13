"""BM25 over structural keys; no model call, embeddings, or response values.

M5.1-B keeps BM25 as the text-relevance core and layers a deterministic feature
gate on top: evidence-derived query features (M5.1-A) are compared against the
optional ``features`` a curated case may declare. BM25 ranks; features only
reject safety-boundary conflicts and lightly adjust the final score. A case
without ``features`` (every case in the frozen corpus) is unaffected, so the
legacy BM25 behaviour is preserved exactly.
"""

import hashlib
import json
from pathlib import Path
import re

from rank_bm25 import BM25Okapi

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


def _query_features(summaries, fields):
    # 延迟导入：features -> retrieval 存在循环依赖，顶层导入会失败。
    from .features import evidence_features
    return evidence_features(summaries, fields or [])


def retrieve(summaries, enabled=True, fields=None, cases=None):
    """BM25 rank curated cases, then gate and lightly re-rank by evidence features.

    ``fields`` are the requested output names (used only for the feature layer);
    ``cases`` is an optional corpus override for deterministic tests. Both are
    additive: the running pipeline calls ``retrieve(summaries, enabled=...)``.
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
        candidates.append((i, float(scores[i]) + match['adjustment']))
    candidates.sort(key=lambda item: (-item[1], cases[item[0]]['id']))
    for i, score in candidates[:2]:
        case = cases[i]
        # 结果条目始终保持 legacy 形状，即使 case 声明了 features。
        result['cases'].append({'id': case['id'], 'title': case['title'], 'terms': case['terms'],
                                'guidance': case['guidance'], 'score': round(score, 6)})
    result['feature_schema_version'] = FEATURE_SCHEMA_VERSION
    result['query_features'] = query_features
    result['gate'] = gate
    return result
