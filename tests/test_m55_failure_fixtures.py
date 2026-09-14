"""M5.5 evidence dataset acceptance; fixtures are not production tasks."""
import hashlib
import json
from pathlib import Path
import importlib.util
import shutil
import subprocess
from unittest.mock import patch
import uuid
import mimetypes
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.app.llm.config import CloudConfig
from backend.app.pipeline.contracts import ExtractionPlan, plan_hash
from backend.app.pipeline.errors import PipelineError, classify
from backend.app.pipeline.repair import APPLICABLE_ERROR_CODES, MAX_REPAIR_ATTEMPTS
from backend.app.storage.repository import ARTIFACT_NAMES
from backend.app.trace.projection import project_trace
from benchmarks import content_sha256
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests/fixtures/failure_cases'
CASES = ('pointer_not_found/unresolved', 'pointer_not_found/applied', 'duplicate_id', 'network_error')
spec = importlib.util.spec_from_file_location('m55_generator', ROOT / 'tests/helpers/m55_failure_fixtures.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def read(case, name):
    return json.loads((DATA / case / name).read_text(encoding='utf-8'))


def projection(case):
    artifacts = {p.name: json.loads(p.read_text(encoding='utf-8'))
                 for p in (DATA / case).glob('*.json') if p.name in ARTIFACT_NAMES}
    return project_trace(read(case, 'task.json'), read(case, 'events.json'), artifacts)


def test_dataset_manifest_and_hashes():
    assert (DATA / 'manifest.json').is_file(), 'Generate the M5.5 fixture dataset first'
    manifest = json.loads((DATA / 'manifest.json').read_text(encoding='utf-8'))
    assert {c['id'] for c in manifest['cases']} == {
        'pointer_not_found/unresolved', 'pointer_not_found/applied',
        'duplicate_id', 'network_error'}
    for case in manifest['cases']:
        assert case['source_type'] == 'generated_integration_fixture'
        assert case['generated_by'] == 'tests/helpers/m55_failure_fixtures.py'
        folder = DATA / case['id']
        assert set(case['hashes']) == {p.name for p in folder.iterdir()}
        assert set(case['expected_artifacts']) <= set(case['hashes'])
        for name, digest in case['hashes'].items():
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('case', CASES)
def test_regeneration_and_safety(case, tmp_path):
    files, artifacts = generator.generate_case(case, tmp_path)
    assert set(files) == {p.name for p in (DATA / case).iterdir()}
    for name, raw in files.items():
        assert raw == (DATA / case / name).read_bytes(), name
    telemetry = json.loads(files['telemetry.json'])
    assert telemetry['planner_stub_calls'] == 1
    assert telemetry['cloud_llm_calls'] == 0
    assert json.loads(files['report.json'])['model_calls'] == 1  # stub count, not cloud usage
    assert len(telemetry['execution_plan_hashes']) == (2 if case.endswith('/applied') else 1)
    assert len(telemetry['http_requests']) == {
        'pointer_not_found/unresolved': 0, 'pointer_not_found/applied': 1,
        'duplicate_id': 2, 'network_error': 1}[case]
    assert set(artifacts) <= ARTIFACT_NAMES


@pytest.mark.parametrize('case,code,category,phase', [
    ('pointer_not_found/unresolved', 'pointer_not_found', 'PLAN_SEMANTIC', 'analyzing'),
    ('duplicate_id', 'duplicate_id', 'DATA_INTEGRITY', 'executing'),
    ('network_error', 'network_error', 'TRANSPORT', 'executing'),
])
def test_failure_classification_and_diagnosis(case, code, category, phase):
    diagnostic = read(case, 'failure_retrieval.json')
    classification = classify(PipelineError(code))
    assert diagnostic['failure_context'] == {'error_code': code, 'category': category, 'phase': phase}
    assert (classification.code, classification.category.value, classification.phase) == (code, category, phase)
    assert set(diagnostic) == {'schema_version', 'failure_context', 'failure_phase',
                               'knowledge_gate', 'case_ids', 'corpus_sha256'}
    assert diagnostic['schema_version'] == 1
    assert diagnostic['knowledge_gate'] == {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}
    assert diagnostic['corpus_sha256'] == read(case, 'retrieval.json')['corpus_sha256']
    assert diagnostic['case_ids'] == [c['id'] for c in read(case, 'retrieval.json')['cases']]
    assert 'http://' not in json.dumps(diagnostic)
    trace = projection(case)
    assert trace['diagnostics']['failure_retrieval']['failure_context'] == diagnostic['failure_context']
    assert trace['diagnostics']['failure_retrieval']['knowledge_gate_applied'] is False
    assert [s['state'] for s in trace['steps']] == ['reached', 'reached', 'failed', 'absent']
    assert read(case, 'report.json')['error_category'] == category
    if case == 'network_error':
        assert read(case, 'report.json')['error_retryable'] is True


@pytest.mark.parametrize('case', CASES)
def test_repair_application_lineage_and_projection(case):
    audit = read(case, 'repair.json')
    report = read(case, 'report.json')
    trace = projection(case)
    assert set(audit) == {'schema_version', 'attempts', 'proposals', 'original_plan_hash',
                         'candidate_plan_hash', 'applied', 'execution_result'}
    assert audit['schema_version'] == 1 and len(audit['attempts']) == 1
    assert audit['original_plan_hash'] == plan_hash(ExtractionPlan(**read(case, 'plan.json')))
    assert trace['repair']['document'] == {'attempts': len(audit['attempts']), 'proposals': len(audit['proposals'])}
    assert trace['repair']['applied'] == audit['applied'] == report['repair_applied']
    if case.endswith('/applied'):
        assert audit['applied'] is True
        assert len(audit['proposals']) == 1
        assert audit['candidate_plan_hash'] == read(case, 'telemetry.json')['execution_plan_hashes'][1]
        assert audit['candidate_plan_hash'] != audit['original_plan_hash']
        assert audit['execution_result']['status'] == report['status'] == 'succeeded'
        assert report['repair_count'] == 1 and report['repair_outcome'] == 'applied'
        assert read(case, 'result.json') == [{'id': 1}]
        assert trace['diagnostics']['failure_retrieval'] is None
        assert not (DATA / case / 'failure_retrieval.json').exists()
        assert [s['state'] for s in trace['steps']] == ['reached'] * 4
    else:
        assert audit['applied'] is False and audit['candidate_plan_hash'] is None
        assert report['repair_count'] == 0 and report['repair_outcome'] == 'rejected'
        assert report['status'] == 'failed'
        if case in ('duplicate_id', 'network_error'):
            assert audit['proposals'] == []
            assert audit['attempts'][0]['rejection_code'] == 'not_repairable'


@pytest.mark.parametrize('case', CASES)
def test_diagnostic_does_not_influence_repair_or_execution(case, tmp_path):
    on, _ = generator.generate_case(case, tmp_path / 'on', True)
    off, _ = generator.generate_case(case, tmp_path / 'off', False)
    assert on['repair.json'] == off['repair.json']
    assert on['telemetry.json'] == off['telemetry.json']
    assert 'failure_retrieval.json' not in off


@pytest.fixture(scope='module')
def compiled_viewer(tmp_path_factory):
    """Build only into pytest temp storage; no installed bundle or source edits."""
    output = tmp_path_factory.mktemp('m55-viewer')
    frontend = ROOT / 'frontend'
    subprocess.run(['node', str(frontend / 'node_modules/vue-tsc/bin/vue-tsc.js'), '--noEmit'],
                   cwd=frontend, check=True, capture_output=True, encoding='utf-8')
    subprocess.run(['node', str(frontend / 'node_modules/vite/bin/vite.js'), 'build',
                    '--configLoader', 'runner', '--outDir', str(output)],
                   cwd=frontend, check=True, capture_output=True, encoding='utf-8')
    yield output


@pytest.mark.parametrize('case', CASES)
def test_trace_api_reads_fixture_without_writes(case, tmp_path, compiled_viewer):
    def config():
        return CloudConfig('https://example.test', 'fixture-only', 'fixture-planner-stub')

    async def forbidden_worker(*args):
        raise AssertionError('read-only fixture must never execute a worker')

    app = create_app(tmp_path, config, forbidden_worker)
    with TestClient(app) as client:
        repo = app.state.repo
        with patch('backend.app.storage.repository.uuid.uuid4',
                   return_value=uuid.UUID(read(case, 'task.json')['id'])):
            task = repo.create({'url': 'http://127.0.0.1:8100/', 'fields': ['id'], 'max_pages': 2},
                               'fixture-planner-stub')
        task_id = task['id']
        folder = tmp_path / 'tasks' / task_id
        folder.mkdir(parents=True)
        for p in (DATA / case).iterdir():
            if p.name in ARTIFACT_NAMES:
                shutil.copyfile(p, folder / p.name)
        for event in read(case, 'events.json')[1:]:
            repo.transition(task_id, event['status'], read(case, 'report.json')
                            if event['status'] in ('failed', 'succeeded') else None)
        repo.register_artifacts(task_id)
        before = (repo.get(task_id), repo.events(task_id), repo.artifacts(task_id))
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        response = client.get(f'/api/v1/tasks/{task_id}/trace')
        assert response.status_code == 200
        trace = response.json()
        expected = projection(case)
        for key in ('status', 'repair', 'diagnostics', 'outcome', 'model_calls'):
            assert trace[key] == expected[key]
        assert [s['state'] for s in trace['steps']] == [s['state'] for s in expected['steps']]
        for entry in before[2]:
            if entry['filename'].endswith('.json'):
                result = client.get(f"/api/v1/tasks/{task_id}/artifacts/{entry['filename']}")
                assert result.status_code == 200
                assert result.json() == read(case, entry['filename'])
        # Browser sees the actual API responses, not handwritten trace JSON.
        # Routing is an in-process transport bridge, never a production server.
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            requests, errors = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route_request(route):
                request = route.request
                requests.append(request.method)
                assert request.method == 'GET'
                url = urlsplit(request.url)
                assert url.hostname == 'm55-fixture.test'
                if url.path.startswith('/api/'):
                    result = client.get(url.path)
                    route.fulfill(status=result.status_code, body=result.content,
                                  content_type=result.headers.get('content-type', 'application/json'))
                else:
                    relative = url.path.removeprefix('/console/').strip('/') or 'index.html'
                    file = (compiled_viewer / relative).resolve()
                    assert file.is_relative_to(compiled_viewer.resolve())
                    if not file.is_file():
                        route.fulfill(status=404, body='Not found')
                    else:
                        mime = 'application/javascript' if file.suffix == '.js' else mimetypes.guess_type(file)[0]
                        route.fulfill(body=file.read_bytes(), content_type=mime or 'application/octet-stream')

            try:
                page.route('**/*', route_request)
                page.goto(f'http://m55-fixture.test/console/#/{task_id}/trace')
                expect(page.get_by_test_id('task-status')).to_have_text(read(case, 'report.json')['status'])
                audit_panel = page.locator('#repair')
                if case.endswith('/applied'):
                    expect(audit_panel).to_contain_text(read(case, 'repair.json')['candidate_plan_hash'])
                    expect(audit_panel).to_contain_text('valid')
                    expect(audit_panel.locator('dd').filter(has_text='true')).to_have_count(2)
                    expect(page.locator('#diagnosis')).to_contain_text('未记录诊断产物')
                else:
                    context = read(case, 'failure_retrieval.json')['failure_context']
                    for value in context.values():
                        expect(page.locator('#diagnosis')).to_contain_text(value)
                    expect(page.locator('#diagnosis')).to_contain_text('Advisory only')
                    expect(audit_panel).to_contain_text('rejected')
                    if case in ('duplicate_id', 'network_error'):
                        expect(audit_panel).to_contain_text('not_repairable')
                expect(audit_panel.get_by_role('button')).to_have_count(0)
                expect(page.locator('#diagnosis').get_by_role('button')).to_have_count(0)
                page.get_by_role('button', name='刷新快照').click()
                expect(page.get_by_test_id('task-status')).to_have_text(read(case, 'report.json')['status'])
                assert requests and set(requests) == {'GET'}
                assert errors == []
            finally:
                browser.close()
        assert before == (repo.get(task_id), repo.events(task_id), repo.artifacts(task_id))
        assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}


def test_frozen_compatibility_and_viewer_contract():
    assert content_sha256() == '9d4f2787ae1dfc78f8896848609c105b7aefaa2b54e2dc378683ad53cb16e1ad'
    assert (ROOT / 'benchmarks/tasks.sha256').read_text().strip() == content_sha256()
    assert hashlib.sha256((ROOT / 'backend/app/rag/cases.json').read_bytes()).hexdigest() == \
        '678f87ece8bdb0aa4171f5f80b6b289cd773215639b7e30d82dc4953f5cba1f2'
    assert APPLICABLE_ERROR_CODES == frozenset({'pointer_not_found'})
    assert MAX_REPAIR_ATTEMPTS == 1
    assert ARTIFACT_NAMES == {'evidence.json', 'cloud_payload.json', 'plan.json', 'result.json',
                             'report.json', 'report.md', 'retrieval.json', 'collector.py',
                             'collector_result.json', 'collector_verification.json',
                             'repair.json', 'failure_retrieval.json'}
    # Explicit freeze contract requested for this milestone; normalize Windows checkout EOL.
    paths = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', 'v0.5.4-freeze',
                                     '--', 'backend', 'frontend/src', 'benchmarks'], cwd=ROOT).decode().splitlines()
    for path in paths:
        frozen = subprocess.check_output(['git', 'show', f'v0.5.4-freeze:{path}'], cwd=ROOT)
        assert (ROOT / path).read_bytes().replace(b'\r\n', b'\n') == frozen.replace(b'\r\n', b'\n'), path
