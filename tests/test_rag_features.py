"""M5.1-A: deterministic evidence feature extraction.

Covers the required observation shapes plus enum closure, redaction and
determinism. These tests exercise the standalone feature layer only; nothing in
the running pipeline consumes it yet.
"""

import json

from backend.app.rag import features
from backend.app.rag.features import evidence_features


def summary(**overrides):
    base = {'request_id': 'r', 'method': 'GET', 'status': 200, 'query': {}}
    base.update(overrides)
    return base


# --- 1. GET query pagination -------------------------------------------

def test_get_query_pagination_is_recognized():
    observation = summary(method='GET', query={'page': '1', 'page_size': '10'},
                          response_shape={'items': [{'id': '<integer>', 'name': '<string>'}],
                                          'has_next': '<boolean>', 'total': '<integer>'})
    result = evidence_features([observation], ['id', 'name'])
    assert result == {'method': 'GET', 'page_location': 'query', 'items_container': 'nested',
                      'stop_signal': 'boolean_flag', 'continuation_flag': 'present',
                      'field_mapping': 'verbatim', 'ambiguity': 'single_candidate'}


# --- 2. POST json_body pagination --------------------------------------

def test_post_json_body_pagination_is_recognized():
    observation = summary(method='POST', query={},
                          response_shape={'data': {'records': [{'code': '<string>'}],
                                                  'totalCount': '<integer>', 'hasMore': '<boolean>'}},
                          request_body_shape={'page': 2, 'size': 8})
    result = evidence_features([observation], ['code'])
    assert result['method'] == 'POST'
    assert result['page_location'] == 'json_body'
    assert result['items_container'] == 'nested'


# --- 3. root list items container --------------------------------------

def test_root_list_container_is_recognized():
    observation = summary(query={'page': '1'},
                          response_shape=[{'id': '<integer>', 'title': '<string>'}])
    result = evidence_features([observation], ['id'])
    assert result['items_container'] == 'root_list'
    assert result['ambiguity'] == 'single_candidate'
    assert result['stop_signal'] == 'total_int_only'
    assert result['continuation_flag'] == 'absent'


# --- 4. nested container -----------------------------------------------

def test_nested_container_is_recognized():
    observation = summary(query={'pageIndex': '1'},
                          response_shape={'data': {'records': [{'sku': '<string>'}],
                                                  'totalCount': '<integer>'}})
    result = evidence_features([observation], ['sku'])
    assert result['items_container'] == 'nested'
    assert result['page_location'] == 'query'


# --- 5. boolean stop signal --------------------------------------------

def test_boolean_stop_signal_is_recognized():
    observation = summary(response_shape={'rows': [{'id': '<integer>'}],
                                          'more': '<boolean>', 'total': '<integer>'})
    result = evidence_features([observation], ['id'])
    assert result['stop_signal'] == 'boolean_flag'
    assert result['continuation_flag'] == 'present'


# --- 6. total integer stop signal --------------------------------------

def test_total_integer_stop_signal_is_recognized():
    observation = summary(response_shape={'items': [{'id': '<integer>'}], 'total': '<integer>'})
    result = evidence_features([observation], ['id'])
    assert result['stop_signal'] == 'total_int_only'
    assert result['continuation_flag'] == 'absent'


def test_page_count_integer_is_reported_as_ambiguous():
    observation = summary(query={'page': '1'},
                          response_shape={'list': [{'id': '<integer>'}],
                                          'totalPages': '<integer>'})
    result = evidence_features([observation], ['id'])
    assert result['stop_signal'] == 'total_pages_ambiguous'


# --- 7. field alias -----------------------------------------------------

def test_field_alias_requires_mapping():
    observation = summary(response_shape={'rows': [{'sku': '<string>', 'title': '<string>'}],
                                          'more': '<boolean>', 'total': '<integer>'})
    assert evidence_features([observation], ['id', 'name'])['field_mapping'] == 'aliased_required'
    assert evidence_features([observation], ['sku', 'title'])['field_mapping'] == 'verbatim'


# --- 8. sensitive fields never reach the output -------------------------

def test_sensitive_fields_do_not_leak_into_features():
    observation = summary(query={'page': '1', 'token': '4'},
                          response_shape={'password': '<string>', 'api_key': '<string>',
                                          'items': [{'id': '<integer>'}],
                                          'has_next': '<boolean>', 'total': '<integer>'})
    result = evidence_features([observation], ['id'])
    encoded = json.dumps(result)
    for secret in ('token', 'password', 'api_key', '4'):
        assert secret not in encoded
    assert set(result) == set(features.FEATURE_KEYS)
    assert result['field_mapping'] == 'verbatim'


def test_sensitive_only_response_is_not_mapped():
    observation = summary(query={'page': '1'},
                          response_shape={'password': '<string>', 'token': '<string>'})
    result = evidence_features([observation], ['id'])
    # 敏感键被跳过，没有任何可用结构证据，因此既不是 verbatim 也不是 aliased_required。
    assert result['field_mapping'] == 'unknown'


# --- closure, stability and determinism --------------------------------

def test_output_is_closed_vocabulary_and_never_carries_values():
    observation = summary(method='POST', query={'page': '1', 'keyword': 'private-word'},
                          response_shape={'data': {'records': [{'label': '<string>'}],
                                                  'totalCount': '<integer>'}},
                          request_body_shape={'page': 3})
    result = evidence_features([observation], ['label'])
    allowed = {
        'method': features.METHODS + (features.UNKNOWN,),
        'page_location': features.PAGE_LOCATIONS + (features.UNKNOWN,),
        'items_container': features.ITEMS_CONTAINERS + (features.UNKNOWN,),
        'stop_signal': features.STOP_SIGNALS,
        'continuation_flag': features.CONTINUATION_FLAGS + (features.UNKNOWN,),
        'field_mapping': features.FIELD_MAPPINGS + (features.UNKNOWN,),
        'ambiguity': features.AMBIGUITY + (features.UNKNOWN,),
    }
    assert set(result) == set(features.FEATURE_KEYS)
    for key, value in result.items():
        assert value in allowed[key], key
    assert 'private-word' not in json.dumps(result)


def test_multiple_containers_report_ambiguity():
    observations = [summary(request_id='a', response_shape={'items': [{'id': '<integer>'}]}),
                    summary(request_id='b', response_shape={'data': {'records': [{'id': '<integer>'}]}})]
    assert evidence_features(observations, ['id'])['ambiguity'] == 'multiple_candidates'


def test_empty_input_is_unknown_and_does_not_raise():
    assert evidence_features([], []) == {'method': 'unknown', 'page_location': 'unknown',
                                         'items_container': 'unknown', 'stop_signal': 'none',
                                         'continuation_flag': 'unknown', 'field_mapping': 'unknown',
                                         'ambiguity': 'unknown'}


def test_same_input_is_stable_and_not_mutated():
    observations = [summary(method='POST', query={'page': '1'},
                            response_shape={'data': {'records': [{'id': '<integer>'}],
                                                    'hasMore': '<boolean>'}},
                            request_body_shape={'page': 1})]
    fields = ['id']
    before = json.loads(json.dumps(observations))
    first = evidence_features(observations, fields)
    second = evidence_features(observations, fields)
    assert first == second
    assert json.dumps(first) == json.dumps(second)
    assert observations == before
    assert fields == ['id']
