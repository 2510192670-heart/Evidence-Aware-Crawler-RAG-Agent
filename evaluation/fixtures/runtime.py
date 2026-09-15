"""Pure fixture projection. Accept public inputs only; never load case metadata."""
import copy

from backend.app.pipeline.contracts import Observation, cloud_summary, local_origin, SENSITIVE, has_sensitive_keys


def validate_input(value):
    if not isinstance(value, dict) or set(value) != {'fields', 'endpoints'}:
        raise ValueError('invalid_public_input')
    if (not isinstance(value['fields'], list) or not value['fields']
            or not all(isinstance(f, str) and f and not SENSITIVE.search(f) for f in value['fields'])
            or len(set(value['fields'])) != len(value['fields'])):
        raise ValueError('invalid_fields')
    endpoints = value['endpoints']
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError('missing_endpoints')
    origins, ids = set(), set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or set(endpoint) != {
                'request_id', 'method', 'url', 'query', 'request_body', 'responses'}:
            raise ValueError('invalid_endpoint')
        responses = endpoint['responses']
        if not isinstance(responses, list) or not 1 <= len(responses) <= 10:
            raise ValueError('invalid_response_sequence')
        if any(not isinstance(body, (dict, list)) for body in responses):
            raise ValueError('invalid_response_body')
        record = Observation(**{k: v for k, v in endpoint.items() if k != 'responses'}, body=responses[0])
        origins.add(local_origin(record.url))
        if any(SENSITIVE.search(k) for k in record.query) or has_sensitive_keys(record.request_body):
            raise ValueError('sensitive_fixture_request')
        if record.request_id in ids:
            raise ValueError('duplicate_request_id')
        ids.add(record.request_id)
    if len(origins) != 1:
        raise ValueError('cross_origin_fixture')
    def metadata_keys(child):
        if isinstance(child, dict):
            return any(k in {'oracle', 'relevant_case_ids', 'result_sha256', 'expected_outcome',
                             'failure_expectation', 'repair_expectation'} or metadata_keys(v)
                       for k, v in child.items())
        if isinstance(child, list):
            return any(metadata_keys(v) for v in child)
        return False
    if metadata_keys(value):
        raise ValueError('scorer_metadata_in_public_input')


def planner_inputs(public_input):
    validate_input(public_input)
    observations = []
    for endpoint in public_input['endpoints']:
        record = Observation(**{k: v for k, v in endpoint.items() if k != 'responses'},
                             body=endpoint['responses'][0])
        observations.append(cloud_summary(record))
    return {'fields': list(public_input['fields']), 'observations': observations}


def response_for(endpoint, ordinal):
    """Sequence lookup, NOT a pagination executor (cursor/offset are not executed)."""
    if type(ordinal) is not int or not 1 <= ordinal <= len(endpoint['responses']):
        raise ValueError('response_ordinal_out_of_range')
    return copy.deepcopy(endpoint['responses'][ordinal - 1])
