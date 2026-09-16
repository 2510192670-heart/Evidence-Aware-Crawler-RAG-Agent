import pytest
from pydantic import ValidationError

from backend.app.main import TaskInput


def test_task_accepts_description_and_typed_fields():
    task = TaskInput(description='提取商品名称和价格', fields=['name', 'price'],
                     field_specs=[{'name': 'name', 'description': '商品名称', 'type': 'string', 'required': True},
                                  {'name': 'price', 'description': '原始价格', 'type': 'number', 'required': False}],
                     max_records=100)
    assert task.description == '提取商品名称和价格'
    assert task.field_specs[1].required is False


def test_field_spec_must_match_requested_names():
    with pytest.raises(ValidationError):
        TaskInput(fields=['name'], field_specs=[{'name': 'other'}])


def test_requirements_do_not_coerce_or_invent_values():
    from backend.app.pipeline.requirements import FieldSpec, verify_fields
    specs = [FieldSpec(name='title'), FieldSpec(name='price', type='number', required=False)]
    rows, missing = verify_fields([{'title': 'A', 'price': None}], specs)
    assert rows == [{'title': 'A', 'price': None}]
    assert missing == {'title': 0, 'price': 1}
    with pytest.raises(ValueError, match='requested_field_type_mismatch'):
        verify_fields([{'title': 'A', 'price': '12.00'}], specs)
    with pytest.raises(ValueError, match='required_field_missing'):
        verify_fields([{'price': 12}], specs)


def test_sensitive_requirements_rejected():
    with pytest.raises(ValidationError):
        TaskInput(description='Authorization: Bearer not-a-real-secret')


def test_requirement_plan_requires_explicit_support_assessment():
    from backend.app.pipeline.requirements import RequirementPlan, checked_requirement_plan
    base = dict(request_id='one', items_pointer='/items', fields={'id': '/id'},
                unique_key='id', page_parameter='page', has_next_pointer=None, total_pointer='/total')
    with pytest.raises(ValidationError):
        RequirementPlan(**base)
    plan = RequirementPlan(**base, unsupported_requirements=['requested aggregation'])
    with pytest.raises(ValueError) as failure:
        checked_requirement_plan(plan)
    assert failure.value.code == 'unsupported_requirements'
    supported = checked_requirement_plan(RequirementPlan(**base, unsupported_requirements=[]))
    assert 'unsupported_requirements' not in supported.model_dump()


def test_executor_optional_fields_and_record_budget():
    import asyncio
    import httpx
    from backend.app.pipeline.contracts import Observation, ExtractionPlan
    from backend.app.pipeline.execution import execute_plan
    from backend.app.pipeline.requirements import FieldSpec
    record = Observation(request_id='one', url='http://127.0.0.1/api', query={'page': '1'},
                         body={'items': [{'id': 1}, {'id': 2, 'price': 12}], 'total': 2})
    plan = ExtractionPlan(request_id='one', items_pointer='/items', fields={'id': '/id', 'price': '/price'},
                          unique_key='id', page_parameter='page', total_pointer='/total', has_next_pointer=None)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=record.body))) as client:
            result = await execute_plan(client, plan, [record], field_specs=[FieldSpec(name='id'),
                                        FieldSpec(name='price', type='number', required=False)], max_records=1)
            assert result['items'] == [{'id': 1, 'price': None}]
            assert result['completeness'] == 'partial'
            assert result['record_limit_reached'] is True
    asyncio.run(check())


def test_requirements_reach_planner_and_result_report(tmp_path, monkeypatch):
    import asyncio
    import json
    import uuid
    from types import SimpleNamespace
    import httpx
    from backend.app.pipeline import run
    from backend.app.pipeline.contracts import Observation, ExtractionPlan
    payloads = []
    plan = ExtractionPlan(request_id='one', items_pointer='/items', fields={'id': '/id'},
                          unique_key='id', page_parameter='page', total_pointer='/total', has_next_pointer=None)

    class Gateway:
        def __init__(self, config):
            self.calls, self.usage_history = 0, []

        async def generate(self, payload, schema):
            self.calls += 1
            payloads.append(payload)
            return SimpleNamespace(data=plan)

    body = {'items': [{'id': 1}, {'id': 2}], 'total': 2}

    async def observe(*args):
        return [Observation(request_id='one', url='http://127.0.0.1/api', query={'page': '1'}, body=body)]

    real_client = httpx.AsyncClient
    monkeypatch.setattr(run, 'CloudGateway', Gateway)
    monkeypatch.setattr(run, 'observe', observe)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    task_id = str(uuid.uuid4())
    args = SimpleNamespace(url='http://127.0.0.1/', fields='id', max_pages=3, click_text='',
                           description='Extract identifiers', field_specs=[{'name': 'id', 'type': 'integer'}],
                           max_records=1, rag_enabled=False)
    assert asyncio.run(run.run_task(args, SimpleNamespace(model='fixture'), task_id=task_id,
                                   output_root=tmp_path, quiet=True)) == 0
    assert payloads[0]['description'] == 'Extract identifiers'
    report = json.loads((tmp_path / 'tasks' / task_id / 'report.json').read_text())
    assert report['count'] == 1
    assert report['missing_fields'] == {'id': 0}
    assert report['record_limit_reached'] is True
    assert report['collector_exported'] is False
