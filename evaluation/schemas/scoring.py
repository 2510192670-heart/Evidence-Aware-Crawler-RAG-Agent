"""Pure evaluation scores. No I/O, model, service or database access."""
from .hashing import digest

_TYPES = {'integer': int, 'number': float, 'string': str, 'boolean': bool}


def _matches(items, pages, completeness, oracle):
    if (not isinstance(items, list) or type(pages) is not int
            or pages != oracle['page_count'] or len(items) != oracle['record_count']
            or completeness != oracle['completeness']):
        return False
    schema = oracle['schema']
    if any(not isinstance(item, dict) or set(item) != set(schema)
           or any(type(item[key]) is not _TYPES.get(kind) for key, kind in schema.items())
           for item in items):
        return False
    keys = [item[oracle['unique_key']] for item in items]
    if any(type(key) not in (int, str) or key == '' for key in keys) or len(set(keys)) != len(keys):
        return False
    return digest(items) == oracle['result_sha256']


def score_task_result(report, records, oracle, collector=None):
    """Collector flags alone are not proof: independently compare exported results."""
    success = report.get('status') == 'succeeded' and _matches(
        records, report.get('pages'), report.get('completeness'), oracle)
    flags = ('collector_exported', 'collector_execution_success', 'collector_matches_internal_result')
    delivered = bool(success and all(report.get(k) is True for k in flags)
                     and isinstance(collector, dict) and _matches(
                         collector.get('items'), collector.get('pages'),
                         collector.get('completeness'), oracle))
    return {'task_success': bool(success), 'delivery_success': delivered}


def score_expected_rejection(status, code, phase, execution_requests, expectation, exception_type=None):
    """A separate contract score; never becomes a task-success numerator."""
    matched = (code in expectation['allowed_codes'] if expectation['allowed_codes'] else
               code is None and expectation.get('exception_type') is not None
               and exception_type == expectation['exception_type'])
    return (status == 'failed' and matched
            and phase == expectation['phase'] and type(execution_requests) is int
            and 0 <= execution_requests <= expectation['max_execution_requests'])


def score_retrieval(retrieved_ids, relevant_ids):
    if (len(retrieved_ids) > 2 or len(set(retrieved_ids)) != len(retrieved_ids)
            or any(not isinstance(x, str) for x in [*retrieved_ids, *relevant_ids])):
        raise ValueError('invalid_retrieval_ids')
    if not relevant_ids:
        return {'top1_hit': None, 'top2_hit': None, 'mrr_at_2': None}
    ranks = [i for i, item in enumerate(retrieved_ids, 1) if item in relevant_ids]
    rank = min(ranks) if ranks else None
    return {'top1_hit': int(rank == 1), 'top2_hit': int(rank is not None),
            'mrr_at_2': 1 / rank if rank else 0}
