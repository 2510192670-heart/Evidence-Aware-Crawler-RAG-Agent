import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import PipelineError


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
                raise PipelineError('pointer_not_found', details={'pointer': path})
        return data
    except (KeyError, IndexError, TypeError):
        raise PipelineError('pointer_not_found', details={'pointer': path}) from None


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str
    url: str
    # method 是证据属性而非计划输入：POST 只能来自被真实观察到的请求。
    method: Literal['GET', 'POST'] = 'GET'
    query: dict[str, str]
    # request_body 是请求 JSON body；body 保持原义，指响应体。
    request_body: dict | None = None
    body: dict | list
    status: int = 200

    @model_validator(mode='after')
    def check_method_body(self):
        """POST 必须有非空 JSON 对象请求体；GET 不得携带请求体。"""
        if self.method == 'POST':
            if not isinstance(self.request_body, dict) or not self.request_body:
                raise ValueError('post_requires_json_object_body')
        elif self.request_body is not None:
            raise ValueError('get_must_not_carry_request_body')
        return self


def has_sensitive_keys(value, depth=0) -> bool:
    """递归检查键名是否敏感；深度受限，避免不可信结构被无限展开。"""
    if depth > 6:
        # 未扫描的结构不能被判定为安全。
        return True
    if isinstance(value, dict):
        return any(SENSITIVE.search(key) or has_sensitive_keys(child, depth + 1)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(has_sensitive_keys(child, depth + 1) for child in value)
    return False


class ExtractionPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    request_id: str = Field(description='Choose an observed request_id, never invent one.')
    items_pointer: str = Field(description='JSON Pointer to the list, e.g. /items.')
    fields: dict[str, str] = Field(min_length=1, max_length=10, description='Requested output name to JSON Pointer relative to each item.')
    unique_key: str = Field(description='A key in fields representing a unique identifier.')
    pagination_location: Literal['query', 'json_body'] = Field(
        default='query',
        description='Where the observed page number lives: "query" for a GET request, "json_body" for a POST JSON body.')
    page_parameter: str = Field(description='An observed query parameter (pagination_location=query) or a top-level request body member (pagination_location=json_body) holding the page number, starting at 1.')
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


def plan_hash(plan: ExtractionPlan) -> str:
    """计划的内容寻址哈希：同一计划稳定，任何字段变化都会改变。

    规范化使用排序键与紧凑分隔符，使 fields 字典的插入顺序、进程与运行环境都不
    影响结果；因此该哈希可作为「这是否仍是我校验过的那份计划」的确定性凭据。
    """
    canonical = json.dumps(plan.model_dump(), sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def validate_pagination(plan: ExtractionPlan, record: Observation):
    """共享分页校验：页码位置必须与方法一致，且页码成员必须是被观察到的可用整数。

    执行时只允许修改 `page_parameter` 这一个成员，所以这里的判定就是安全边界：
    计划声明的分页位置必须来自证据，不能把观察到的 GET 变成 POST，也不能凭空
    指定一个 body 成员。
    """
    page = plan.page_parameter
    if plan.pagination_location == 'query':
        if record.method != 'GET':
            raise PipelineError('page_location_mismatch', details={'parameter': page})
        observed = record.query.get(page)
        if observed is None or not observed.isdecimal():
            # 沿用既有码的裸 ValueError 行为（该码尚未迁移到 PipelineError）。
            raise ValueError('unobserved_page_parameter')
        return
    if record.method != 'POST':
        raise PipelineError('page_location_mismatch', details={'parameter': page})
    body = record.request_body if isinstance(record.request_body, dict) else {}
    if page not in body:
        raise ValueError('unobserved_page_parameter')
    if type(body[page]) is not int:
        raise PipelineError('invalid_page_field_type', details={'parameter': page})


def validate_plan(plan: ExtractionPlan, record: Observation) -> dict:
    """执行前的确定性计划校验，返回每个字段在样本上的类型。

    执行器与 repair 候选校验共用这一个函数，因此「校验通过的候选」与「可进入
    执行器的计划」完全等价，不存在第二套判定标准。本函数不发起任何请求、不修改
    计划，异常类型与错误码与执行器原有行为逐一保持一致。
    """
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
    return sample_types


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


def request_shape(value):
    """请求体摘要：小页码可见以便定位页码成员，其余只保留类型占位。

    与 query 的处理保持一致——只有短小十进制值（页码/页大小）才原样出现，
    其余字符串、文本值一律降级为类型占位。
    """
    if not isinstance(value, dict):
        return shape(value)
    visible = {}
    for key, item in list(value.items())[:30]:
        if SENSITIVE.search(key):
            continue
        if type(item) is int and 0 < item <= 999:
            visible[key] = item
        elif isinstance(item, str) and item.isdecimal() and len(item) <= 3:
            visible[key] = item
        else:
            visible[key] = shape(item)
    return visible


def cloud_summary(record: Observation):
    summary = {
        'request_id': record.request_id, 'method': record.method, 'status': record.status,
        'query': {k: (v if v.isdecimal() and len(v) <= 3 else '<value>')
                  for k, v in record.query.items() if not SENSITIVE.search(k)},
        'response_shape': shape(record.body),
    }
    if record.method == 'POST':
        # 页码成员必须可识别，否则模型无法判断页码位于 JSON body 中。
        summary['request_body_shape'] = request_shape(record.request_body)
    return summary

