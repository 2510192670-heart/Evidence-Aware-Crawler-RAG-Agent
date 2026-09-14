"""M5.4-A: read-only projection of task evidence into a trace view.

``project_trace`` maps already-persisted evidence -- the task row, the
``task_events`` timeline and the task's decoded JSON artifacts -- onto the
Trace Viewer shape. It is a *read model*, never a second source of truth:

- pure and deterministic: the same inputs always yield a deep-equal result, and
  the inputs are never mutated;
- no model call, no network, no execution and no repair driver: it only reads;
- no writes: the trace is derived on read, so no task artifact is added and the
  frozen ``ARTIFACT_NAMES`` contract is untouched;
- fail closed: missing or malformed evidence degrades to ``absent`` / ``None``;
  nothing is inferred, replayed or reconstructed.

The lifecycle vocabulary (``STAGES`` / ``TERMINAL``) comes from the storage
contract so the projected timeline cannot drift from what the pipeline records.
"""

from collections.abc import Mapping
from datetime import datetime

from ..storage.repository import STAGES, TERMINAL

TRACE_SCHEMA_VERSION = 1

# 步骤状态闭集。ABSENT：任务已结束但该阶段没有证据；PENDING：任务尚未走到该阶段。
# 只有这四个值会被输出，前端契约据此固定。
REACHED = 'reached'
FAILED = 'failed'
PENDING = 'pending'
ABSENT = 'absent'
TRACE_STATES = (REACHED, FAILED, PENDING, ABSENT)

# 只读取这些已注册产物；任何读取失败都由调用方传 None，投影统一按缺失处理。
_REPORT = 'report.json'
_PLAN = 'plan.json'
_EVIDENCE = 'evidence.json'
_RETRIEVAL = 'retrieval.json'
_REPAIR = 'repair.json'
_FAILURE = 'failure_retrieval.json'


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    return value if isinstance(value, str) else None


def _seconds(start, end):
    """Elapsed seconds between two recorded timestamps, or None if unusable."""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def _ordered(events):
    """Events with a usable status, ordered by their recorded sequence."""
    if not isinstance(events, (list, tuple)):
        return []
    rows = [event for event in events
            if isinstance(event, Mapping) and isinstance(event.get('status'), str)]
    return sorted(rows, key=lambda event: event.get('seq') if isinstance(event.get('seq'), int) else 0)


def _index(rows):
    index = {}
    for row in rows:
        index.setdefault(row['status'], row)      # 同一状态只取首次记录
    return index


def _durations(rows):
    durations = {}
    previous = None
    for row in rows:
        at = row.get('created_at')
        if previous is not None and row['status'] not in durations:
            durations[row['status']] = _seconds(previous, at)
        previous = at
    return durations


def _retrieval(value):
    if not isinstance(value, Mapping):
        return None
    gate, cases = value.get('gate'), value.get('cases')
    return {'enabled': value.get('enabled') if isinstance(value.get('enabled'), bool) else None,
            'algorithm': _text(value.get('algorithm')),
            'gate_applied': gate.get('applied') if isinstance(gate, Mapping)
            and isinstance(gate.get('applied'), bool) else None,
            'case_ids': [case['id'] for case in cases
                         if isinstance(case, Mapping) and isinstance(case.get('id'), str)]
            if isinstance(cases, list) else []}


def _evidence(stage, report, artifacts):
    """Evidence for one reached stage; every value comes straight from an artifact."""
    if stage == 'observing':
        observations = artifacts.get(_EVIDENCE)
        return {'source': _text(report.get('source')),
                'observations': len(observations) if isinstance(observations, list) else None}
    if stage == 'analyzing':
        plan = _mapping(artifacts.get(_PLAN))
        fields = plan.get('fields')
        return {'model_calls': _integer(report.get('model_calls')),
                'plan_fields': [field for field in fields if isinstance(field, str)]
                if isinstance(fields, list) else None,
                'retrieval': _retrieval(artifacts.get(_RETRIEVAL))}
    if stage == 'executing':
        result = artifacts.get('result.json')
        return {'count': len(result) if isinstance(result, list) else _integer(report.get('count')),
                'pages': _integer(report.get('pages')),
                'repair_attempted': report.get('repair_attempted')
                if isinstance(report.get('repair_attempted'), bool) else None}
    if stage == 'verifying':
        return {'completeness': _text(report.get('completeness')),
                'expected_total': _integer(report.get('expected_total'))}
    return None


def _steps(status, terminal, index, durations, report, artifacts):
    stages = [stage for stage in STAGES if stage != 'created']
    reached = [stage for stage in stages if stage in index]
    last = reached[-1] if reached else None
    steps = []
    for stage in stages:
        if stage not in index:
            # 没有证据就明确标缺失，不猜测是否执行过。
            state = ABSENT if terminal else PENDING
            steps.append({'key': stage, 'state': state, 'at': None, 'duration_s': None, 'evidence': None})
            continue
        state = FAILED if (stage == last and terminal and status != 'succeeded') else REACHED
        at = index[stage].get('created_at')
        steps.append({'key': stage, 'state': state, 'at': _text(at),
                      'duration_s': durations.get(stage),
                      'evidence': _evidence(stage, report, artifacts)})
    return steps


def _outcome(report):
    return {'status': _text(report.get('status')), 'count': _integer(report.get('count')),
            'pages': _integer(report.get('pages')), 'completeness': _text(report.get('completeness')),
            'expected_total': _integer(report.get('expected_total')), 'error': _text(report.get('error')),
            'error_type': _text(report.get('error_type')),
            'error_category': _text(report.get('error_category')),
            'error_repairable': report.get('error_repairable')
            if isinstance(report.get('error_repairable'), bool) else None}


def _repair(report, artifacts):
    document = artifacts.get(_REPAIR)
    attempts = document.get('attempts') if isinstance(document, Mapping) else None
    proposals = document.get('proposals') if isinstance(document, Mapping) else None
    counted = {'attempts': len(attempts), 'proposals': len(proposals)} \
        if isinstance(attempts, list) and isinstance(proposals, list) else None
    return {'attempted': report.get('repair_attempted')
            if isinstance(report.get('repair_attempted'), bool) else None,
            'count': _integer(report.get('repair_count')),
            'outcome': _text(report.get('repair_outcome')),
            'applied': report.get('repair_applied')
            if isinstance(report.get('repair_applied'), bool) else None,
            'document': counted}


def _diagnostics(artifacts):
    """M5.3 failure-branch evidence, projected read-only; absent stays null."""
    value = artifacts.get(_FAILURE)
    if not isinstance(value, Mapping):
        return {'failure_retrieval': None}
    gate, context, case_ids = value.get('knowledge_gate'), value.get('failure_context'), value.get('case_ids')
    return {'failure_retrieval': {
        'schema_version': _integer(value.get('schema_version')),
        'failure_phase': _text(value.get('failure_phase')),
        'failure_context': {'error_code': _text(context.get('error_code')),
                            'category': _text(context.get('category')),
                            'phase': _text(context.get('phase'))} if isinstance(context, Mapping) else None,
        'case_ids': [case for case in case_ids if isinstance(case, str)] if isinstance(case_ids, list) else [],
        'knowledge_gate_applied': gate.get('applied') if isinstance(gate, Mapping)
        and isinstance(gate.get('applied'), bool) else None}}


def _artifact_index(entries):
    if not isinstance(entries, (list, tuple)):
        return []
    index = []
    for entry in entries:
        name = entry.get('filename') if isinstance(entry, Mapping) else None
        if not isinstance(name, str):
            continue
        index.append({'filename': name, 'sha256': _text(entry.get('sha256')),
                      'size_bytes': _integer(entry.get('size_bytes'))})
    return sorted(index, key=lambda item: item['filename'])


def project_trace(task, events=None, artifacts=None, artifact_index=None):
    """Project persisted evidence onto the trace view (pure, deterministic).

    ``task`` is the task row, ``events`` the ``task_events`` timeline, and
    ``artifacts`` maps an artifact filename to its already-decoded JSON (or
    ``None`` when it is missing or unreadable); ``artifact_index`` is the
    registered-artifact index. Every argument is optional and untrusted: a
    malformed value degrades to ``absent`` / ``None`` instead of raising.
    """
    task = _mapping(task)
    artifacts = _mapping(artifacts)
    report = _mapping(artifacts.get(_REPORT))
    rows = _ordered(events)
    status = _text(task.get('status'))
    terminal = status in TERMINAL
    return {
        'trace_schema_version': TRACE_SCHEMA_VERSION,
        'task_id': _text(task.get('id')),
        'status': status,
        'phase': 'terminal' if terminal else 'running',
        'source': _text(report.get('source')),
        'model': _text(task.get('model')),
        'model_calls': _integer(report.get('model_calls')),
        'elapsed_seconds': _number(report.get('elapsed_seconds')),
        'timeline_available': bool(rows),
        'created_at': _text(task.get('created_at')),
        'steps': _steps(status, terminal, _index(rows), _durations(rows), report, artifacts),
        'outcome': _outcome(report),
        'repair': _repair(report, artifacts),
        'diagnostics': _diagnostics(artifacts),
        'artifacts': _artifact_index(artifact_index),
    }
