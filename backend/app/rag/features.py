"""Deterministic evidence features for observation-aware retrieval (M5.1-A).

Pure, closed-vocabulary feature extraction over already-redacted observation
summaries. The function inspects key names and type placeholders only and emits
one enum per feature; it never returns response values, query values or request
body values, performs no I/O and makes no model call.

This module is additive: it does not import, modify or replace
``backend/app/rag/retrieval.py``. M5.1-B may consume its output; until then it
is a standalone layer that nothing in the running pipeline calls.
"""

from ..pipeline.contracts import SENSITIVE
from .retrieval import tokens

UNKNOWN = 'unknown'

METHODS = ('GET', 'POST')
PAGE_LOCATIONS = ('query', 'json_body')
ITEMS_CONTAINERS = ('root_list', 'nested')
STOP_SIGNALS = ('boolean_flag', 'total_int_only', 'total_pages_ambiguous', 'none')
CONTINUATION_FLAGS = ('present', 'absent')
FIELD_MAPPINGS = ('verbatim', 'aliased_required')
AMBIGUITY = ('single_candidate', 'multiple_candidates')

FEATURE_KEYS = ('method', 'page_location', 'items_container', 'stop_signal',
                'continuation_flag', 'field_mapping', 'ambiguity')

# 类型占位符由 contracts.shape()/request_shape() 产生，这里只识别它们。
_BOOLEAN = '<boolean>'
_INTEGER = '<integer>'

# 键名含这些 token 时，整数更可能是「页数」而不是「记录总数」。
_PAGE_KEY_TOKENS = frozenset({'page', 'pages', 'pageno', 'pagecount',
                              'pageindex', 'totalpages'})


def _members(node):
    """展开响应结构里所有 dict 成员 (键名, 子结构/类型占位)，跳过敏感键。"""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str) or SENSITIVE.search(key):
                    continue
                yield key, child
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)


def _containers(response_shape):
    """候选 items 容器的签名集合；签名只是键名路径，不含任何值。

    根是列表 -> ``()``；dict 成员是列表 -> 该成员的键名路径。
    """
    found = set()

    def walk(node, path):
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or SENSITIVE.search(key):
                    continue
                walk(child, path + (key,))
        elif isinstance(node, list):
            if path:
                found.add(path)
            for child in node:
                walk(child, path)

    if isinstance(response_shape, list):
        found.add(())
    walk(response_shape, ())
    return found


def _normalized(value):
    return ''.join(tokens(value))


def _method(summaries):
    seen = {summary.get('method') for summary in summaries
            if summary.get('method') in METHODS}
    return seen.pop() if len(seen) == 1 else UNKNOWN


def _query_has_number(summary):
    query = summary.get('query')
    if not isinstance(query, dict):
        return False
    return any(isinstance(value, str) and value.isdecimal() and len(value) <= 3
               for key, value in query.items()
               if isinstance(key, str) and not SENSITIVE.search(key))


def _body_has_number(summary):
    body = summary.get('request_body_shape')
    if not isinstance(body, dict):
        return False
    for key, value in body.items():
        if not isinstance(key, str) or SENSITIVE.search(key):
            continue
        if type(value) is int and 0 < value <= 999:
            return True
        if isinstance(value, str) and value.isdecimal() and len(value) <= 3:
            return True
    return False


def _page_location(summaries, method):
    query_number = any(_query_has_number(summary) for summary in summaries)
    body_number = any(_body_has_number(summary) for summary in summaries)
    if method == 'POST':
        return 'json_body' if body_number else ('query' if query_number else UNKNOWN)
    if method == 'GET':
        return 'query' if query_number else ('json_body' if body_number else UNKNOWN)
    if query_number and not body_number:
        return 'query'
    if body_number and not query_number:
        return 'json_body'
    return UNKNOWN


def _stop_signal(summaries):
    bool_present = page_int = record_int = False
    for summary in summaries:
        for key, value in _members(summary.get('response_shape')):
            if value == _BOOLEAN:
                bool_present = True
            elif value == _INTEGER:
                if _PAGE_KEY_TOKENS.intersection(tokens(key)):
                    page_int = True
                else:
                    record_int = True
    if bool_present:
        return 'boolean_flag'
    if page_int:
        return 'total_pages_ambiguous'
    if record_int:
        return 'total_int_only'
    return 'none'


def _continuation_flag(summaries, stop_signal):
    if not any(isinstance(summary.get('response_shape'), (dict, list))
               and summary.get('response_shape') for summary in summaries):
        return UNKNOWN
    return 'present' if stop_signal == 'boolean_flag' else 'absent'


def _field_mapping(summaries, fields):
    requested = [_normalized(field) for field in fields
                 if isinstance(field, str) and field.strip()]
    if not requested or any(not name for name in requested):
        return UNKNOWN
    available = set()
    for summary in summaries:
        available.update(_normalized(key) for key, _ in _members(summary.get('response_shape')))
    if not available:
        return UNKNOWN
    return 'verbatim' if all(name in available for name in requested) else 'aliased_required'


def evidence_features(summaries, fields):
    """Derive closed-vocabulary features from redacted observation summaries.

    ``summaries`` are the already-redacted ``cloud_summary`` dicts; ``fields`` are
    the requested output field names. Returns exactly :data:`FEATURE_KEYS`, each
    value drawn from its declared enum (or ``unknown``). Deterministic for equal
    input; no response, query or request body value is ever returned.
    """
    summaries = [summary for summary in (summaries or []) if isinstance(summary, dict)]
    fields = list(fields or [])

    signatures = set()
    for summary in summaries:
        signatures.update(_containers(summary.get('response_shape')))
    if not signatures:
        items_container = UNKNOWN
        ambiguity = UNKNOWN
    else:
        items_container = 'root_list' if () in signatures else 'nested'
        ambiguity = 'single_candidate' if len(signatures) == 1 else 'multiple_candidates'

    method = _method(summaries)
    stop_signal = _stop_signal(summaries)
    return {
        'method': method,
        'page_location': _page_location(summaries, method),
        'items_container': items_container,
        'stop_signal': stop_signal,
        'continuation_flag': _continuation_flag(summaries, stop_signal),
        'field_mapping': _field_mapping(summaries, fields),
        'ambiguity': ambiguity,
    }
