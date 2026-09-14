"""Run with pytest; requires Vite at TRACE_BASE_URL (default localhost:5173).

Fixtures use the frozen read-model projector. Only browser GET responses are
stubbed; no task, model call, or task artifact is created.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from playwright.sync_api import sync_playwright, expect
from backend.app.trace.projection import project_trace

BASE = os.getenv('TRACE_BASE_URL', 'http://127.0.0.1:5173/console/')


def fixture(failed=False, diagnostic=True):
    names = ['observing', 'analyzing', 'executing']
    if not failed:
        names += ['verifying']
    names += ['failed' if failed else 'succeeded']
    task = {'id': 'demo', 'status': names[-1], 'model': 'fixture-model'}
    events = [{'seq': i, 'status': name, 'created_at': f'2026-09-14T10:00:0{i}'}
              for i, name in enumerate(names)]
    docs = {'evidence.json': [{'request_id': 'req-1'}],
            'retrieval.json': {'enabled': True, 'algorithm': 'BM25Okapi', 'cases': []},
            'result.json': [{'id': 1}],
            'report.json': {'status': names[-1], 'count': 1, 'pages': 1,
                            'model_calls': 0, 'completeness': 'complete'}}
    if failed:
        docs['report.json'].update(error='pointer_not_found', error_category='PLAN_SEMANTIC')
        docs['repair.json'] = {'attempts': [{'reason_code': 'eligible_pointer_not_found'}],
                               'proposals': [], 'candidate_plan_hash': 'abc123', 'applied': False}
        if diagnostic:
            docs['failure_retrieval.json'] = {
                'failure_context': {'error_code': 'pointer_not_found', 'category': 'PLAN_SEMANTIC', 'phase': 'executing'},
                'failure_phase': 'execution', 'case_ids': ['case-A'],
                'knowledge_gate': {'applied': True, 'matched': ['case-A'], 'filtered': ['case-B']}}
    entries = [{'filename': name, 'size_bytes': 100, 'sha256': 'a' * 64} for name in docs]
    return project_trace(task, events, docs, entries), docs


@pytest.fixture
def browser_page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        yield page
        assert not errors
        browser.close()


def mount(page, trace, docs, artifact_status=200):
    requests = []
    def api(route):
        requests.append(route.request.method)
        path = route.request.url.split('/api/v1')[-1]
        if path.endswith('/trace'):
            route.fulfill(status=200 if trace else 404, json=trace or {'code': 'task_not_found'})
        elif '/artifacts/' in path:
            route.fulfill(status=artifact_status, json=docs.get(path.rsplit('/', 1)[-1], {'code': 'artifact_changed'}))
        else:
            route.fulfill(json={'items': [], 'total': 0, 'model': {'configured': False}})
    page.route('**/api/v1/**', api)
    page.goto(BASE + '#/demo/trace')
    return requests


def test_success_and_artifact(browser_page):
    page = browser_page
    trace, docs = fixture()
    requests = mount(page, trace, docs)
    expect(page.get_by_role('heading', name='Task Trace Viewer')).to_be_visible()
    expect(page.locator('.trace-step[data-state="reached"]')).to_have_count(4)
    expect(page.get_by_test_id('task-status')).to_have_text('succeeded')
    expect(page.get_by_test_id('model-calls')).to_have_text('0')
    page.get_by_role('button', name='打开 result.json', exact=True).click()
    expect(page.get_by_test_id('artifact-content')).to_contain_text('id')
    assert set(requests) == {'GET'}


def test_failure_diagnosis_and_repair(browser_page):
    page = browser_page
    requests = mount(page, *fixture(True))
    expect(page.locator('.trace-step[data-state="failed"]')).to_contain_text('Executing')
    expect(page.locator('#diagnosis')).to_contain_text('case-A')
    expect(page.locator('#diagnosis')).to_contain_text('case-B')
    expect(page.locator('#diagnosis')).to_contain_text('Advisory only')
    expect(page.locator('#repair')).to_contain_text('abc123')
    expect(page.locator('#repair')).to_contain_text('eligible_pointer_not_found')
    expect(page.locator('.trace-step[data-state="absent"]')).to_have_count(1)
    assert set(requests) == {'GET'}


def test_no_diagnosis(browser_page):
    mount(browser_page, *fixture(True, False))
    expect(browser_page.locator('#diagnosis')).to_contain_text('未记录诊断产物')


def test_unknown_task(browser_page):
    mount(browser_page, None, {})
    expect(browser_page.get_by_role('alert')).to_contain_text('404')
    expect(browser_page.get_by_role('button', name='重试')).to_be_visible()


def test_artifact_integrity_error(browser_page):
    mount(browser_page, *fixture(), artifact_status=409)
    browser_page.get_by_role('button', name='打开 result.json', exact=True).click()
    expect(browser_page.get_by_test_id('artifact-content')).to_contain_text('409')


def test_pending_and_missing_values(browser_page):
    trace = project_trace({'id': 'demo', 'status': 'observing'}, [
        {'seq': 1, 'status': 'observing'}])
    mount(browser_page, trace, {})
    expect(browser_page.locator('.trace-step[data-state="pending"]')).to_have_count(3)
    expect(browser_page.get_by_test_id('model-calls')).to_have_text('未记录')
    expect(browser_page.locator('#artifacts')).to_contain_text('暂无已注册产物')


def test_switch_task_clears_previous_artifact_and_error(browser_page):
    page = browser_page
    trace, docs = fixture()
    mount(page, trace, docs)
    page.get_by_role('button', name='打开 result.json', exact=True).click()
    expect(page.get_by_test_id('artifact-content')).to_contain_text('id')
    page.evaluate("location.hash = '/other/trace'")
    expect(page.get_by_test_id('artifact-content')).to_have_count(0)
    expect(page.get_by_role('heading', name='Task Trace Viewer')).to_be_visible()


def test_delayed_trace_does_not_replace_new_task(browser_page):
    page = browser_page
    trace, _ = fixture()
    held = []
    def api(route):
        if '/slow/' in route.request.url:
            held.append(route)
        else:
            route.fulfill(json={**trace, 'task_id': 'new-task'})
    page.route('**/api/v1/**', api)
    page.goto(BASE + '#/slow/trace')
    expect(page.get_by_role('status')).to_contain_text('正在读取')
    page.evaluate("location.hash = '/new-task/trace'")
    expect(page.locator('.overview-heading')).to_contain_text('new-task')
    assert held
    held[0].fulfill(json={**trace, 'task_id': 'slow'})
    expect(page.locator('.overview-heading')).to_contain_text('new-task')


def test_json_is_text_and_collapsible(browser_page):
    page = browser_page
    trace, docs = fixture()
    docs['result.json'] = [{'html': '<img src=x onerror="window.injected=true">'}]
    mount(page, trace, docs)
    page.get_by_role('button', name='打开 result.json', exact=True).click()
    content = page.get_by_test_id('artifact-content')
    expect(content).to_contain_text('<img src=x')
    assert page.evaluate('window.injected') is None
    content.locator('summary').first.click()
    expect(content.locator('.json-leaf')).to_have_count(0)
    content.locator('summary').first.click()
    expect(content).to_contain_text('<img src=x')


def test_desktop_and_small_screen_layout(browser_page):
    page = browser_page
    mount(page, *fixture(True))
    expect(page.locator('#repair')).to_contain_text('abc123')
    for width in (1440, 1024, 390):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


@pytest.mark.parametrize('evidence', [{}, {'retrieval': None}])
def test_missing_retrieval_display(browser_page, evidence):
    trace, docs = fixture()
    trace['steps'][1]['evidence'] = evidence
    mount(browser_page, trace, docs)
    section = browser_page.locator('section').filter(has=browser_page.get_by_role('heading', name='Retrieval Insight'))
    expect(section.locator('.json-leaf')).to_have_text('retrieval: 未记录')


def test_summary_formatter_preserves_false_zero_and_objects(browser_page):
    trace, docs = fixture()
    trace['model'] = ''
    trace['steps'][0]['evidence'] = {'missing': None, 'empty': '', 'enabled': False, 'count': 0, 'cases': [], 'nested': {'value': 42}}
    mount(browser_page, trace, docs)
    section = browser_page.locator('#stage-observing')
    expect(section).to_contain_text('missing: 未记录')
    expect(section).to_contain_text('empty: 未记录')
    expect(section).to_contain_text('enabled: false')
    expect(section).to_contain_text('count: 0')
    expect(section).to_contain_text('无记录')
    expect(section).to_contain_text('value: 42')
    expect(browser_page.locator('.trace-metadata')).to_contain_text('Model 未记录')


def test_raw_artifact_preserves_null_empty_array_and_string(browser_page):
    trace, docs = fixture()
    docs['result.json'] = {'missing': None, 'empty': '', 'items': [], 'enabled': False, 'count': 0}
    mount(browser_page, trace, docs)
    browser_page.get_by_role('button', name='打开 result.json', exact=True).click()
    content = browser_page.get_by_test_id('artifact-content')
    expect(content).to_contain_text('missing: null')
    expect(content).to_contain_text('empty: ""')
    expect(content).to_contain_text('[]')
    expect(content).to_contain_text('enabled: false')
    expect(content).to_contain_text('count: 0')
    expect(content).not_to_contain_text('未记录')
    expect(content).not_to_contain_text('无记录')


if __name__ == '__main__':
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), '-q', *sys.argv[1:]]))
