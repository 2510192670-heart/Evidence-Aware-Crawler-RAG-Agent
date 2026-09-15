"""Use frozen production validators/executor on controlled inputs, not a runner.

Plans below are explicitly test-authored, never read from scorer oracles.
MockTransport supplies only fixture wire responses; execution remains real code.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend.app.pipeline.contracts import ExtractionPlan, Observation, validate_plan
from backend.app.pipeline.execution import execute_plan
from backend.app.pipeline.errors import PipelineError, classify
from backend.app.pipeline.repair import RepairProposer, is_applicable
from backend.app.rag.failure import build_failure_context
from evaluation.fixtures.runtime import response_for
from evaluation.schemas.contract import read_json, validate_bundle
from evaluation.schemas.scoring import score_task_result, score_expected_rejection

ROOT = Path(__file__).resolve().parents[1]
CASES = ['get_flat', 'duplicate_id', 'total_changed', 'field_type_changed',
         'empty_page_with_next', 'post_nested', 'get_nested', 'multi_endpoint',
         'cursor_boundary', 'offset_boundary', 'pointer_applied', 'pointer_ambiguous']


@pytest.fixture(autouse=True)
def forbid_cloud_and_uncontrolled_http(monkeypatch):
    from backend.app.llm.gateway import CloudGateway
    async def forbidden_async(*args, **kwargs):
        raise AssertionError('M6.1 must not send a cloud or uncontrolled HTTP request')
    def forbidden_sync(*args, **kwargs):
        raise AssertionError('M6.1 must not send an uncontrolled HTTP request')
    monkeypatch.setattr(CloudGateway, 'generate', forbidden_async)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden_sync)


def authored_plan(name, request_id):
    kwargs = dict(request_id=request_id, items_pointer='/items', fields={'id': '/id', 'value': '/value'},
                  unique_key='id', page_parameter='page', total_pointer='/total', has_next_pointer='/more')
    if name == 'post_nested':
        kwargs.update(items_pointer='/data/rows', fields={'id': '/code', 'value': '/detail/label'},
                      pagination_location='json_body', total_pointer='/data/total', has_next_pointer='/data/more')
    elif name == 'get_nested':
        kwargs.update(items_pointer='/payload/entries', fields={'id': '/key', 'value': '/meta/text'},
                      total_pointer='/payload/count', has_next_pointer=None)
    elif name.startswith('pointer_'):
        kwargs.update(items_pointer='/missing', has_next_pointer=None)
    elif name.endswith('_boundary'):
        kwargs.update(total_pointer=None, has_next_pointer=None)
    return ExtractionPlan(**kwargs)


@pytest.mark.parametrize('name', CASES)
def test_real_validator_and_executor_match_authored_expectation(name):
    case = next(c for c in validate_bundle(ROOT) if c.id == 'm6_' + name)
    public = read_json(ROOT, case.fixture.ref)
    endpoint = public['endpoints'][0]
    record = Observation(**{k: v for k, v in endpoint.items() if k != 'responses'}, body=endpoint['responses'][0])
    plan = authored_plan(name, record.request_id)
    requests = []

    def respond(request):
        assert request.url.host == '127.0.0.1'
        assert request.url.path == '/' + endpoint['url'].rsplit('/', 1)[1]
        assert request.method == endpoint['method']
        if request.method == 'POST':
            body = json.loads(request.content)
            ordinal = body.pop('page')
            assert body == {k: v for k, v in endpoint['request_body'].items() if k != 'page'}
            assert dict(request.url.params) == endpoint['query']
        else:
            ordinal = int(request.url.params['page'])
            assert {k: v for k, v in request.url.params.items() if k != 'page'} == {
                k: v for k, v in endpoint['query'].items() if k != 'page'}
        requests.append(ordinal)
        return httpx.Response(200, json=response_for(endpoint, ordinal))

    async def run_controlled():
        candidate = plan
        if name.startswith('pointer_'):
            with pytest.raises(PipelineError) as caught:
                validate_plan(plan, record)
            proposed, proposal = RepairProposer().evaluate(caught.value, plan, record)
            if name == 'pointer_ambiguous':
                assert proposed is None
                raise caught.value
            assert proposed is not None and is_applicable(caught.value, proposal)
            candidate = proposed
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
            return await execute_plan(client, candidate, [record], max_pages=3)

    # Runtime input and execution above have no oracle access.
    if case.expected_outcome == 'success':
        result = asyncio.run(run_controlled())
        oracle = read_json(ROOT, case.oracle.ref)
        score = score_task_result({'status': 'succeeded', 'pages': result['pages'],
                                   'completeness': result['completeness']}, result['items'], oracle)
        assert score == {'task_success': True, 'delivery_success': False}
        assert len(requests) == oracle['page_count']
    else:
        with pytest.raises(ValueError) as caught:
            asyncio.run(run_controlled())
        actual = classify(caught.value)
        expectation = case.failure_expectation.model_dump()
        assert score_expected_rejection('failed', actual.code if actual.code in expectation['allowed_codes'] else None,
                                        actual.phase, len(requests), expectation, type(caught.value).__name__)
        if name.endswith('_boundary'):
            assert requests == []
            assert actual.category.value == 'UNCLASSIFIED'
            assert build_failure_context(caught.value) is None
