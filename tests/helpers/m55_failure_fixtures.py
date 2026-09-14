"""Generate isolated evidence with the real pipeline; never import into production.

Run from repository root: python tests/helpers/m55_failure_fixtures.py
Only the fixed tests/fixtures/failure_cases destination is written by the CLI.
Observation/planner/HTTP/export are test doubles. Execution, repair, taxonomy,
retrieval and artifact serialization are production implementations.
"""
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.pipeline.errors import PipelineError

CASES = ('pointer_not_found/unresolved', 'pointer_not_found/applied',
         'duplicate_id', 'network_error')
GENERATED_BY = 'tests/helpers/m55_failure_fixtures.py'


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def generate_case(case_id, root, rag_enabled=True):
    """Run one controlled task into caller-owned temporary storage.

    Metadata timestamps are fixed fixture coordinates, not measured timings.
    The run module's elapsed-time clock is fixed without patching asyncio's clock.
    """
    if case_id not in CASES:
        raise ValueError('unknown fixture')
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'm55-fixture/' + case_id))
    body = {'items': [{'id': 1}], 'total': 1}
    if case_id.endswith('/unresolved'):
        body['other'] = [{'id': 2}]  # two candidates: deterministic refusal
    plan = ExtractionPlan(request_id='req_001', fields={'id': '/id'}, unique_key='id',
                          items_pointer='/missing' if case_id.startswith('pointer_') else '/items',
                          page_parameter='page', total_pointer='/total', has_next_pointer=None)
    observation = Observation(request_id='req_001', url='http://127.0.0.1:8100/items',
                              query={'page': '1'}, body=body)
    telemetry = {'planner_stub_calls': 0, 'cloud_llm_calls': 0,
                 'http_requests': [], 'execution_plan_hashes': [], 'stages': []}

    class Gateway:
        calls = 0
        usage_history = []

        def __init__(self, config):
            pass

        async def generate(self, payload, schema):
            self.calls += 1
            telemetry['planner_stub_calls'] += 1
            if self.calls != 1:
                raise AssertionError('second planning call forbidden')
            return SimpleNamespace(data=plan)

    async def observe(*args):
        return [observation]

    def response(request):
        assert request.url.host == '127.0.0.1' and request.method == 'GET'
        telemetry['http_requests'].append({'method': request.method, 'page': request.url.params['page']})
        if case_id == 'network_error':
            # Explicit classified injection; does NOT test httpx -> taxonomy mapping.
            raise PipelineError('network_error')
        payload = {'items': [{'id': 1}], 'total': 1}
        if case_id == 'duplicate_id':
            payload['total'] = 2  # same ID on page 2 reaches the real duplicate detector
        return httpx.Response(200, json=payload)

    original_client = httpx.AsyncClient
    original_execute = run.execute_plan

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(response))

    async def execute(*args, **kwargs):
        telemetry['execution_plan_hashes'].append(run.plan_hash(args[1]))
        return await original_execute(*args, **kwargs)

    async def export(*args):
        # Export is outside this dataset; never claim collector verification.
        return {'collector_exported': False, 'collector_execution_success': False,
                'collector_matches_internal_result': False}

    async def stage(name):
        telemetry['stages'].append(name)

    def forbid_sync_http(*args, **kwargs):
        raise AssertionError('external HTTP forbidden in fixture generation')

    async def forbid_async_http(*args, **kwargs):
        raise AssertionError('external HTTP forbidden in fixture generation')

    with ExitStack() as stack:
        for name, replacement in [('CloudGateway', Gateway), ('observe', observe),
                                  ('execute_plan', execute), ('export_collector', export),
                                  ('time', SimpleNamespace(monotonic=lambda: 0.0))]:
            stack.enter_context(patch.object(run, name, replacement))
        stack.enter_context(patch.object(httpx, 'AsyncClient', client))
        stack.enter_context(patch.object(httpx.HTTPTransport, 'handle_request', forbid_sync_http))
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', forbid_async_http))
        args = SimpleNamespace(url='http://127.0.0.1:8100/', click_text='', fields='id',
                               max_pages=2, rag_enabled=rag_enabled)
        asyncio.run(run.run_task(args, SimpleNamespace(model='fixture-planner-stub'),
                                task_id=task_id, output_root=root, on_stage=stage, quiet=True))
    folder = Path(root) / 'tasks' / task_id
    artifacts = {p.name: p.read_bytes() for p in sorted(folder.iterdir())}
    report = json.loads(artifacts['report.json'])
    statuses = ['created', *telemetry['stages'], report['status']]
    events = [{'seq': i + 1, 'status': s, 'created_at': f'2000-01-01T00:00:{i:02d}+00:00'}
              for i, s in enumerate(statuses)]
    metadata = {
        'task.json': {'id': task_id, 'status': report['status'], 'model': 'fixture-planner-stub',
                      'created_at': events[0]['created_at']},
        'events.json': events,
        'inputs.json': {'observation': observation.model_dump(), 'initial_plan': plan.model_dump(),
                        'network_injection': case_id == 'network_error'},
        'telemetry.json': telemetry,
    }
    files = {**artifacts, **{k: encode(v) for k, v in metadata.items()}}
    return files, list(artifacts)


def main():
    destination = ROOT / 'tests/fixtures/failure_cases'
    manifest = {'schema_version': 1, 'production_task': False,
                'code_baseline': '643220810ad1e648b36bf3c69b2fc6313df5d283',
                'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'limitations': ['fixed fixture timestamps and elapsed time',
                                'planner/observation/HTTP/export doubles; no live provider',
                                'network_error is classified injection, not a real network incident'],
                'cases': []}
    with tempfile.TemporaryDirectory(prefix='m55-generation-') as temporary:
        for case_id in CASES:
            files, artifacts = generate_case(case_id, Path(temporary) / case_id)
            folder = destination / case_id
            folder.mkdir(parents=True, exist_ok=True)
            unexpected = {p.name for p in folder.iterdir()} - set(files)
            if unexpected:
                raise RuntimeError(f'refuse stale files: {unexpected}')
            for name, raw in files.items():
                (folder / name).write_bytes(raw)
            manifest['cases'].append({'id': case_id, 'source_type': 'generated_integration_fixture',
                                      'generated_by': GENERATED_BY, 'expected_artifacts': artifacts,
                                      'hashes': {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}})
    (destination / 'manifest.json').write_bytes(encode(manifest))
    print('Generated four integration fixtures; no production task or cloud call.')


if __name__ == '__main__':
    main()
