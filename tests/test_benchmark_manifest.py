"""Integrity of the frozen dev / held-out benchmark manifest.

The manifest is ground truth, so these tests only check that it is complete,
internally consistent, isolated from the RAG corpus, and frozen by hash. They
must not weaken when a scenario becomes executable.
"""

import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from backend.app.pipeline.contracts import local_origin, pointer
from backend.app.rag.retrieval import tokens
from benchmarks import HASH_PATH, SCENARIOS, SPLITS, content_sha256, load
from fixtures.app import DEFAULT_PORT, app

client = TestClient(app)

MANIFEST = load()
TASKS = MANIFEST['tasks']
CASES_PATH = Path(__file__).resolve().parents[1] / 'backend' / 'app' / 'rag' / 'cases.json'

SOLUTION_KEYS = {'method', 'request_path', 'pagination_parameter', 'page_size_parameter',
                 'items_pointer', 'has_next_pointer', 'total_pointer', 'unique_key'}
TASK_KEYS = {'id', 'scenario', 'split', 'entry_url', 'fields', 'page_size', 'max_pages',
             'solution', 'expected'}


def split(split_name):
    return [task for task in TASKS if task['split'] == split_name]


def route_tokens(path):
    return set(tokens(path))


def corpus_tokens():
    cases = json.loads(CASES_PATH.read_text(encoding='utf-8'))
    found = set()
    for case in cases:
        for value in case.values():
            for text in (value if isinstance(value, list) else [value]):
                found.update(tokens(str(text)))
    return found


# --- shape and counts ---------------------------------------------------

def test_manifest_has_twenty_tasks_in_five_scenarios():
    assert MANIFEST['schema_version'] == 1
    assert len(TASKS) == 20
    for scenario in SCENARIOS:
        subset = [task for task in TASKS if task['scenario'] == scenario]
        assert len(subset) == 4, scenario
        assert [task['split'] for task in subset].count('dev') == 2, scenario
        assert [task['split'] for task in subset].count('held_out') == 2, scenario


def test_task_ids_are_unique_and_prefixed_by_scenario():
    ids = [task['id'] for task in TASKS]
    assert len(set(ids)) == len(ids)
    for task in TASKS:
        assert task['id'].startswith(task['scenario'].lower() + '-')


def test_each_task_declares_a_complete_schema():
    for task in TASKS:
        assert set(task) == TASK_KEYS, task['id']
        solution = task['solution']
        assert set(solution) == SOLUTION_KEYS, task['id']
        assert solution['method'] in {'GET', 'POST'}, task['id']
        assert solution['request_path'].startswith('/'), task['id']
        assert solution['unique_key'] in task['fields'], task['id']
        assert 1 <= task['page_size'] <= 100, task['id']
        assert 1 <= task['max_pages'] <= 10, task['id']
        assert task['entry_url'].startswith('http://127.0.0.1:'), task['id']

        expected = task['expected']
        if not expected['supported']:
            assert set(expected) == {'supported', 'unsupported_until', 'record_count', 'pages', 'completeness'}
            assert expected['unsupported_until'] == 'M4.3', task['id']
        elif expected['should_fail']:
            assert set(expected) == {'supported', 'should_fail', 'failure_kind'}, task['id']
        else:
            assert set(expected) == {'supported', 'should_fail', 'record_count', 'pages', 'completeness'}, task['id']
            assert expected['completeness'] == 'complete', task['id']
            assert expected['record_count'] > 0 and expected['pages'] >= 1, task['id']


def test_post_tasks_are_declared_supported_after_m4_3():
    # M4.3.3 完成后 S2 POST JSON 进入能力边界：不再保留 unsupported_until。
    post_tasks = [task for task in TASKS if task['solution']['method'] == 'POST']
    assert post_tasks
    for task in post_tasks:
        expected = task['expected']
        assert expected['supported'] is True, task['id']
        assert 'unsupported_until' not in expected, task['id']
        assert expected['should_fail'] is False, task['id']
        assert expected['completeness'] == 'complete', task['id']


# --- dev / held-out isolation -------------------------------------------

def test_dev_and_held_out_route_names_are_disjoint():
    dev = set().union(*[route_tokens(task['solution']['request_path']) for task in split('dev')])
    held = set().union(*[route_tokens(task['solution']['request_path']) for task in split('held_out')])
    assert dev.isdisjoint(held)


def test_dev_and_held_out_field_names_are_disjoint():
    dev = {field for task in split('dev') for field in task['fields']}
    held = {field for task in split('held_out') for field in task['fields']}
    assert dev.isdisjoint(held)


def test_dev_and_held_out_pagination_keys_are_disjoint():
    dev = {task['solution']['pagination_parameter'] for task in split('dev')}
    held = {task['solution']['pagination_parameter'] for task in split('held_out')}
    assert dev.isdisjoint(held)


def test_held_out_names_never_appear_in_the_rag_corpus():
    names = set()
    for task in split('held_out'):
        solution = task['solution']
        names.update(tokens(solution['request_path']))
        names.update(tokens(solution['pagination_parameter']))
        names.update(tokens(solution['page_size_parameter']))
        for field in task['fields']:
            names.update(tokens(field))
    assert names.isdisjoint(corpus_tokens())


# --- answer key vs. fixtures --------------------------------------------

def test_entry_urls_are_literal_loopback_and_served():
    assert MANIFEST['fixture_default_port'] == DEFAULT_PORT
    for task in TASKS:
        url = task['entry_url']
        assert local_origin(url) == f'http://127.0.0.1:{DEFAULT_PORT}', task['id']
        assert urlsplit(url).query == '', task['id']
        assert client.get(urlsplit(url).path).status_code == 200, task['id']


def test_solution_pointers_resolve_on_the_first_page():
    for task in TASKS:
        solution = task['solution']
        params = {solution['pagination_parameter']: 1, solution['page_size_parameter']: task['page_size']}
        if solution['method'] == 'POST':
            body = client.post(solution['request_path'], json=params).json()
        else:
            body = client.get(solution['request_path'], params=params).json()
        items = pointer(body, solution['items_pointer'])
        assert isinstance(items, list) and items, task['id']
        assert len(items) <= task['page_size'], task['id']
        assert type(pointer(body, solution['total_pointer'])) is int, task['id']
        if solution['has_next_pointer'] is not None:
            assert type(pointer(body, solution['has_next_pointer'])) is bool, task['id']
        assert solution['unique_key'] in items[0], task['id']


# --- versioning ---------------------------------------------------------

def test_content_hash_matches_frozen_metadata():
    recorded = HASH_PATH.read_text(encoding='utf-8').strip()
    assert len(recorded) == 64
    assert content_sha256(MANIFEST) == recorded
