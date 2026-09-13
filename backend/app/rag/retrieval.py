"""BM25 over structural keys; no model call, embeddings, or response values."""

import hashlib
import json
from pathlib import Path
import re

from rank_bm25 import BM25Okapi


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


def retrieve(summaries, enabled=True):
    raw = Path(__file__).with_name('cases.json').read_bytes()
    cases = json.loads(raw)
    result = {'enabled': enabled, 'algorithm': 'BM25Okapi', 'corpus_version': 1,
              'corpus_sha256': hashlib.sha256(raw).hexdigest(), 'cases': []}
    if not enabled:
        return result
    query = sorted(set(token for summary in summaries for section in ('query', 'response_shape')
                       for token in structure_keys(summary.get(section, {}))))
    corpus = [tokens(' '.join(case['terms'])) for case in cases]
    scores = BM25Okapi(corpus).get_scores(query)
    ranked = sorted(range(len(cases)), key=lambda i: (-float(scores[i]), cases[i]['id']))
    for i in ranked:
        if scores[i] <= 0 or not set(query).intersection(corpus[i]):
            continue
        result['cases'].append({**cases[i], 'score': round(float(scores[i]), 6)})
        if len(result['cases']) == 2:
            break
    return result
