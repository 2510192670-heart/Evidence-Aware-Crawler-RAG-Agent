import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


SENSITIVE = re.compile(r'password|passwd|secret|token|authorization|cookie|api.?key|email|phone', re.I)


def local_origin(url: str) -> str:
    """此切片只支持字面量 loopback，避免 localhost DNS/公网范围歧义。"""
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('only_literal_loopback_http_supported')
    _ = parsed.port
    return f'{parsed.scheme}://{parsed.netloc}'


def pointer(data, path: str):
    if path == '':
        return data
    if not path.startswith('/') or re.search(r'~(?![01])', path):
        raise ValueError('invalid_json_pointer')
    try:
        for part in path[1:].split('/'):
            key = part.replace('~1', '/').replace('~0', '~')
            if isinstance(data, list):
                if not re.fullmatch(r'0|[1-9][0-9]*', key):
                    raise ValueError('invalid_array_index')
                data = data[int(key)]
            elif isinstance(data, dict):
                data = data[key]
            else:
                raise ValueError('pointer_not_found')
        return data
    except (KeyError, IndexError, TypeError):
        raise ValueError('pointer_not_found') from None


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str
    url: str
    query: dict[str, str]
    body: dict | list
    status: int = 200


class ExtractionPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    request_id: str = Field(description='Choose an observed request_id, never invent one.')
    items_pointer: str = Field(description='JSON Pointer to the list, e.g. /items.')
    fields: dict[str, str] = Field(min_length=1, max_length=10, description='Requested output name to JSON Pointer relative to each item.')
    unique_key: str = Field(description='A key in fields representing a unique identifier.')
    page_parameter: str = Field(description='An observed query parameter holding page number, starting at 1.')
    has_next_pointer: str | None = Field(description='JSON Pointer to boolean next-page flag, or null if absent.')
    total_pointer: str | None = Field(description='JSON Pointer to total item count, or null if absent.')

    @field_validator('items_pointer', 'has_next_pointer', 'total_pointer')
    @classmethod
    def check_pointer(cls, value):
        if value is not None and (value != '' and not value.startswith('/') or re.search(r'~(?![01])', value)):
            raise ValueError('invalid_json_pointer')
        if value and SENSITIVE.search(value):
            raise ValueError('sensitive_pointer')
        return value


def shape(value, depth=0):
    """仅输出字段形状，不输出任意响应值；敏感字段不出现在云摘要中。"""
    if depth > 6:
        return '<depth_limit>'
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:30] if not SENSITIVE.search(k)}
    if isinstance(value, list):
        return [shape(item, depth + 1) for item in value[:1]]
    if value is None:
        return '<null>'
    if type(value) is bool:
        return '<boolean>'
    if type(value) is int:
        return '<integer>'
    if type(value) is float:
        return '<number>'
    return '<string>'


def cloud_summary(record: Observation):
    return {
        'request_id': record.request_id, 'method': 'GET', 'status': record.status,
        'query': {k: (v if v.isdecimal() and len(v) <= 3 else '<value>')
                  for k, v in record.query.items() if not SENSITIVE.search(k)},
        'response_shape': shape(record.body),
    }
