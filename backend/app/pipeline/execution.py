import json
import math

import httpx

from .contracts import (ExtractionPlan, Observation, SENSITIVE, has_sensitive_keys, local_origin, pointer,
                        validate_pagination)
from .errors import PipelineError


async def execute_plan(client: httpx.AsyncClient, plan: ExtractionPlan,
                       observations: list[Observation], max_pages: int = 3):
    if not 1 <= max_pages <= 10:
        raise ValueError('max_pages_out_of_range')
    record = next((r for r in observations if r.request_id == plan.request_id), None)
    if record is None:
        raise ValueError('unknown_request_id')
    local_origin(record.url)
    if any(SENSITIVE.search(k) for k in record.query):
        raise ValueError('sensitive_request_not_supported')
    if has_sensitive_keys(record.request_body):
        raise ValueError('sensitive_request_not_supported')
    validate_pagination(plan, record)
    if plan.unique_key not in plan.fields:
        raise ValueError('unique_key_not_in_fields')
    for name, path in plan.fields.items():
        if not path.startswith('/') or SENSITIVE.search(name) or SENSITIVE.search(path):
            raise ValueError('invalid_or_sensitive_field')
    sample = pointer(record.body, plan.items_pointer)
    if not isinstance(sample, list) or not sample:
        raise ValueError('no_list_sample')
    sample_types = {name: type(pointer(sample[0], path)) for name, path in plan.fields.items()}
    if plan.total_pointer is not None:
        if type(pointer(record.body, plan.total_pointer)) is not int:
            raise ValueError('invalid_total_pointer')
    if plan.has_next_pointer is not None:
        if type(pointer(record.body, plan.has_next_pointer)) is not bool:
            raise ValueError('invalid_has_next_pointer')

    collected = []
    seen = set()
    expected_total = None
    for page in range(1, max_pages + 1):
        # 每页只修改 page_parameter 一个成员；其余成员与观察到的 query 原样回放。
        if plan.pagination_location == 'json_body':
            request_body = dict(record.request_body)
            request_body[plan.page_parameter] = page
            stream = client.stream('POST', record.url, params=record.query, json=request_body,
                                   timeout=10, follow_redirects=False)
        else:
            query = dict(record.query)
            query[plan.page_parameter] = str(page)
            stream = client.stream('GET', record.url, params=query, timeout=10, follow_redirects=False)
        async with stream as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > 256 * 1024:
                    raise ValueError('response_too_large')
                raw.extend(chunk)
        body = json.loads(raw)
        items = pointer(body, plan.items_pointer)
        if not isinstance(items, list):
            raise ValueError('items_not_list')
        if plan.total_pointer is not None:
            total = pointer(body, plan.total_pointer)
            if type(total) is not int or total < 0:
                raise ValueError('invalid_total')
            if expected_total is not None and total != expected_total:
                raise PipelineError('total_changed', details={'page': page})
            expected_total = total
        for item in items:
            output = {name: pointer(item, path) for name, path in plan.fields.items()}
            for name, value in output.items():
                if type(value) not in {int, float, str, bool} or type(value) != sample_types[name]:
                    raise PipelineError('field_type_changed', details={'page': page, 'field': name})
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError('non_finite_number')
            key = output[plan.unique_key]
            if type(key) not in {int, str} or key == '':
                raise ValueError('invalid_unique_key')
            if key in seen:
                raise PipelineError('duplicate_id', details={'page': page, 'unique_key': plan.unique_key})
            seen.add(key)
            collected.append(output)
        if expected_total is not None and len(collected) > expected_total:
            raise ValueError('too_many_items')
        if plan.has_next_pointer is not None:
            more = pointer(body, plan.has_next_pointer)
            if type(more) is not bool:
                raise ValueError('invalid_has_next')
        else:
            more = bool(items) and (expected_total is None or len(collected) < expected_total)
        if more and not items:
            raise PipelineError('empty_page_with_next', details={'page': page})
        if not more:
            if expected_total is not None and len(collected) != expected_total:
                raise ValueError('incomplete_items')
            return {'items': collected, 'pages': page, 'expected_total': expected_total,
                    'completeness': 'complete' if expected_total is not None else 'stop_condition_only'}
    return {'items': collected, 'pages': max_pages, 'expected_total': expected_total, 'completeness': 'partial'}
