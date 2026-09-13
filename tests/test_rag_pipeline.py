import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.app.pipeline import run
from backend.app.pipeline.contracts import ExtractionPlan, Observation


@pytest.mark.parametrize('enabled', [True, False])
def test_rag_reaches_prompt_and_artifacts_without_extra_calls(tmp_path, monkeypatch, enabled):
    captured = []
    class Gateway:
        calls = 0
        usage_history = []
        def __init__(self, config):
            pass
        async def generate(self, payload, schema):
            captured.append(payload)
            self.calls += 1
            return SimpleNamespace(data=ExtractionPlan(request_id='r', items_pointer='/items',
                fields={'id': '/id'}, unique_key='id', page_parameter='page',
                has_next_pointer='/has_next', total_pointer='/total'))
    async def observe(*args):
        return [Observation(request_id='r', url='http://127.0.0.1:8000/api/products',
            query={'page': '1'}, body={'items': [{'id': 1}], 'has_next': False, 'total': 1})]
    async def execute(*args):
        return {'items': [{'id': 1}], 'pages': 1, 'completeness': 'complete', 'expected_total': 1}
    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(run, 'execute_plan', execute)
    args = SimpleNamespace(url='http://127.0.0.1:8000/', click_text='', fields='id', max_pages=1, rag_enabled=enabled)
    assert asyncio.run(run.run_task(args, SimpleNamespace(model='fake'), output_root=tmp_path, quiet=True)) == 0
    folder = next((tmp_path / 'tasks').iterdir())
    report = json.loads((folder / 'report.json').read_text())
    retrieval = json.loads((folder / 'retrieval.json').read_text(encoding='utf-8'))
    assert report['model_calls'] == len(captured) == 1
    assert bool(retrieval['cases']) is enabled
    assert ('reference_cases' in captured[0]) is enabled
    assert retrieval['enabled'] is enabled
