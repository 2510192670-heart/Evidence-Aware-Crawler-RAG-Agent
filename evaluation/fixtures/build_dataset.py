"""Deterministic asset builder, not an evaluation runner.

build_assets returns text without writing. The CLI writes only absent assets or
identical assets; changing a reviewed file requires an explicit authoring edit.
"""
import copy
import json
from pathlib import Path

from backend.app.pipeline.errors import SPECS
from backend.app.rag.knowledge import project_repairability
from evaluation.schemas.contract import Case, family_signature
from evaluation.schemas.hashing import digest, source_digest

BASE = 'evaluation/datasets/m6-v1/'
ROOT = Path(__file__).resolve().parents[2]


def endpoint(name, responses, method='GET', query=None, body=None):
    return {'request_id': 'req_' + name, 'method': method,
            'url': 'http://127.0.0.1:18161/' + name,
            'query': {'page': '1'} if query is None else query,
            'request_body': body, 'responses': responses}


def success_oracle(records, pages):
    return {'records': records, 'page_count': pages, 'record_count': len(records),
            'result_sha256': digest(records), 'schema': {'id': 'integer', 'value': 'string'},
            'unique_key': 'id', 'completeness': 'complete', 'expected_error_code': None,
            'expected_error_phase': None, 'expected_exception_type': None}


def failure_oracle(code):
    return {'records': None, 'page_count': None, 'record_count': None,
            'result_sha256': None, 'schema': {}, 'unique_key': None, 'completeness': None,
            'expected_error_code': code, 'expected_error_phase': SPECS[code].phase if code else 'unknown',
            'expected_exception_type': None if code else 'ValueError'}


def corpus():
    """Authored general guidance; no task answers, output values or query labels."""
    rows = [
        ('flat_get', 'GET total and next flag', ['page', 'items', 'total', 'more'],
         {'method': 'GET', 'page_location': 'query'}, ['duplicate_id', 'total_changed', 'field_type_changed', 'empty_page_with_next']),
        ('nested_get', 'Nested GET container', ['page', 'payload', 'entries', 'count'],
         {'method': 'GET', 'page_location': 'query', 'items_container': 'nested'}, []),
        ('nested_post', 'JSON body paging', ['page', 'data', 'rows', 'more', 'total'],
         {'method': 'POST', 'page_location': 'json_body', 'items_container': 'nested'}, ['page_location_mismatch']),
        ('candidate_get', 'Observed endpoint selection', ['page', 'items', 'total', 'more'],
         {'method': 'GET', 'page_location': 'query'}, []),
        ('pointer_get', 'Observed direct-list pointer', ['page', 'records', 'other'],
         {'method': 'GET', 'page_location': 'query'}, ['pointer_not_found']),
        ('boundary', 'Unsupported continuation', ['cursor', 'offset', 'next_cursor', 'next_offset'],
         {'method': 'GET', 'page_location': 'query'}, []),
    ]
    result = []
    for id_, title, terms, features, codes in rows:
        result.append({'id': id_, 'title': title, 'terms': terms,
                       'guidance': 'Use observed request semantics only; never invent a parameter or bypass validation.',
                       'features': features,
                       'failure_modes': [{'code': code, 'category': SPECS[code].category.value,
                                          'phase': SPECS[code].phase, 'signal': 'observed_failure'} for code in codes],
                       'repairability': {code: project_repairability(code) for code in codes}})
    return result


def specifications():
    """Explicitly authored wire responses and separate scorer answers."""
    records = [{'id': 1, 'value': 'alpha'}, {'id': 2, 'value': 'beta'},
               {'id': 3, 'value': 'gamma'}, {'id': 4, 'value': 'delta'}]
    flat = [{'items': records[:2], 'total': 4, 'more': True},
            {'items': records[2:], 'total': 4, 'more': False}]
    entries = []
    def add(id_, family, split, categories, endpoints, answer, relevant, *,
            outcome='success', supported=True, injection='none', trigger=None, applied=False):
        entries.append(dict(id=id_, family=family, split=split, categories=categories,
                            fixture={'fields': ['id', 'value'], 'endpoints': endpoints}, oracle=answer,
                            relevant=relevant, outcome=outcome, supported=supported,
                            injection=injection, trigger=trigger, applied=applied))

    # All flat-envelope behavior variants stay in ONE development family.
    canary = copy.deepcopy(flat)
    canary[0]['secret'] = 'M6_CANARY_' + 'SECRET_001'
    add('get_flat', 'flat_total_flag', 'development', ['collection', 'get', 'canary'],
        [endpoint('listing', canary)], success_oracle(records, 2), ['flat_get'])
    for code in ('duplicate_id', 'total_changed', 'field_type_changed', 'empty_page_with_next'):
        responses = copy.deepcopy(flat)
        if code == 'duplicate_id':
            responses[1]['items'][0]['id'] = 1
        elif code == 'total_changed':
            responses[1]['total'] = 5
        elif code == 'field_type_changed':
            responses[1]['items'][0]['value'] = 99
        else:
            responses[1]['items'], responses[1]['more'] = [], True
        add(code, 'flat_total_flag', 'development', ['get', 'data_integrity', code, 'diagnostic'],
            [endpoint('listing', responses)], failure_oracle(code), ['flat_get'],
            outcome='expected_rejection', injection='response_drift', trigger=code)

    # Structurally different held-side families; aliases alone do not create a family.
    post = [{'data': {'rows': [{'code': 10, 'detail': {'label': 'ten'}}], 'total': 2, 'more': True}},
            {'data': {'rows': [{'code': 11, 'detail': {'label': 'eleven'}}], 'total': 2, 'more': False}}]
    add('post_nested', 'post_nested_with_query', 'frozen_evaluation', ['collection', 'post', 'nested', 'alias'],
        [endpoint('query', post, 'POST', {'locale': 'en'}, {'page': 1, 'filter': 'active'})],
        success_oracle([{'id': 10, 'value': 'ten'}, {'id': 11, 'value': 'eleven'}], 2), ['nested_post'])
    nested = [{'payload': {'entries': [{'key': 20, 'meta': {'text': 'twenty'}}], 'count': 2}},
              {'payload': {'entries': [{'key': 21, 'meta': {'text': 'twenty_one'}}], 'count': 2}}]
    add('get_nested', 'get_nested_total_only', 'frozen_evaluation', ['collection', 'get', 'nested', 'alias'],
        [endpoint('inventory', nested)],
        success_oracle([{'id': 20, 'value': 'twenty'}, {'id': 21, 'value': 'twenty_one'}], 2), ['nested_get'])
    add('multi_endpoint', 'get_multi_candidate', 'frozen_evaluation', ['collection', 'get', 'multiple_candidates'],
        [endpoint('catalog', copy.deepcopy(flat)), endpoint('summary', [{'count': 4, 'label': 'summary'}])],
        success_oracle(records, 2), ['candidate_get', 'flat_get'])
    for mode in ('cursor', 'offset'):
        wire = [{'items': [{'id': 30, 'value': 'thirty'}], 'next_' + mode: 'continuation'}]
        add(mode + '_boundary', mode + '_continuation', 'frozen_evaluation',
            ['get', 'unsupported', mode, 'unclassified', 'diagnostic'],
            [endpoint('continuation', wire, query={mode: 'start' if mode == 'cursor' else '0'})],
            failure_oracle(None), ['boundary'], outcome='expected_rejection', supported=False,
            injection='unsupported_page_plan')
    for ambiguous in (False, True):
        response = {'records': [{'id': 40, 'value': 'forty'}], 'total': 1}
        if ambiguous:
            response['other'] = [{'id': 41, 'value': 'forty_one'}]
        add('pointer_ambiguous' if ambiguous else 'pointer_applied', 'direct_list_pointer', 'frozen_evaluation',
            ['get', 'repair', 'pointer_not_found', 'ambiguous' if ambiguous else 'applied', 'diagnostic'],
            [endpoint('records', [response])],
            failure_oracle('pointer_not_found') if ambiguous else success_oracle([{'id': 40, 'value': 'forty'}], 1),
            ['pointer_get'], outcome='controlled_failure' if ambiguous else 'success',
            injection='missing_items_pointer', trigger='pointer_not_found', applied=not ambiguous)
    return entries


def build_assets():
    objects, cases, families = {}, [], {}
    for item in specifications():
        id_ = 'm6_' + item['id']
        input_ref = BASE + 'inputs/' + id_ + '.json'
        oracle_ref = BASE + 'oracles/' + id_ + '.json'
        labels_ref = BASE + 'labels/' + id_ + '.json'
        case_ref = BASE + 'cases/' + id_ + '.json'
        objects[input_ref], objects[oracle_ref] = item['fixture'], item['oracle']
        objects[labels_ref] = {'relevant_case_ids': item['relevant'],
                              'failure_relevant_case_ids': item['relevant'] if item['trigger'] else [],
                              'rationale': 'Authored from request structure and failure policy before retrieval; not derived from rank.'}
        code = item['oracle']['expected_error_code']
        trigger = item['trigger']
        disposition = project_repairability(trigger) if trigger else 'none'
        case = {
            'id': id_, 'family_id': item['family'], 'version': 1, 'split': item['split'],
            'categories': item['categories'], 'evidence_level': 'component_fixture',
            'supported': item['supported'], 'expected_outcome': item['outcome'],
            'fixture': {'ref': input_ref, 'fixture_id': id_, 'generator_version': 'm61-a-1'},
            'request': {'ref': input_ref, 'method': item['fixture']['endpoints'][0]['method']},
            'oracle': {'ref': oracle_ref}, 'retrieval_labels': {'ref': labels_ref},
            'failure_expectation': {'classified': code is not None, 'allowed_codes': [code] if code else [],
                                    'phase': item['oracle']['expected_error_phase'],
                                    'exception_type': item['oracle']['expected_exception_type'],
                                    'max_execution_requests': 2 if item['injection'] == 'response_drift' else 0},
            'repair_expectation': {'trigger_code': trigger, 'policy_disposition': disposition,
                                   'eligible': disposition in ('auto_applied', 'proposal_only'),
                                   'may_apply': disposition == 'auto_applied', 'expected_applied': item['applied'],
                                   'max_attempts': 1 if disposition in ('auto_applied', 'proposal_only') else 0},
            'provenance': {'source': 'm6_authored', 'natural_failure': False,
                           'injected_failure': item['injection'] != 'none', 'injection': item['injection'],
                           'note': 'Authored evaluation source; no natural model failure and no production task.'},
            'hashes': {'fixture': digest(objects[input_ref]), 'oracle': digest(objects[oracle_ref]),
                       'retrieval_labels': digest(objects[labels_ref])}}
        Case.model_validate(case)
        objects[case_ref] = case
        cases.append(case_ref)
        signature = family_signature(item['fixture'])
        family = {'id': item['family'], 'split': item['split'], 'signature': signature}
        if item['family'] in families and families[item['family']] != family:
            raise ValueError('family_authoring_conflict')
        families[item['family']] = family
    family_ref = BASE + 'families.json'
    objects[family_ref] = sorted(families.values(), key=lambda x: x['id'])
    full = corpus()
    objects['evaluation/corpora/m6-v1/e3.json'] = full
    objects['evaluation/corpora/m6-v1/e2.json'] = [{k: v for k, v in c.items() if k not in ('failure_modes', 'repairability')} for c in full]
    objects['evaluation/corpora/m6-v1/e1.json'] = [{k: v for k, v in c.items() if k not in ('features', 'failure_modes', 'repairability')} for c in full]
    objects['evaluation/schemas/case.schema.json'] = Case.model_json_schema()
    sources = ['evaluation/fixtures/build_dataset.py', 'evaluation/fixtures/runtime.py',
               'evaluation/schemas/contract.py', 'evaluation/schemas/hashing.py', 'evaluation/schemas/scoring.py']
    manifest = {'version': 1, 'status': 'm61_a_review', 'baseline': '15c5678a963665412f5e68e29617ddd7469b844d',
                'cases': sorted(cases), 'families': family_ref,
                'files': {name: digest(value) for name, value in sorted(objects.items())},
                'sources': {name: source_digest((ROOT / name).read_text(encoding='utf-8')) for name in sources}}
    objects[BASE + 'manifest.json'] = manifest
    texts = {name: json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
             for name, value in objects.items()}
    texts[BASE + 'manifest.sha256'] = digest(manifest) + '\n'
    return texts


if __name__ == '__main__':
    assets = build_assets()
    for name, text in assets.items():
        path = ROOT / name
        if path.exists() and path.read_text(encoding='utf-8') != text:
            raise SystemExit('Refuse to overwrite reviewed asset: ' + name)
    for name, text in assets.items():
        path = ROOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
    print('Built 12 M6.1-A review cases; not a frozen m6-v1 release.')
