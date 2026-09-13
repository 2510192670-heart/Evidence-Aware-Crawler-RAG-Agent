"""Pipeline error taxonomy.

Classification is decided by the raise site and resolved from a single
registry: a call site passes a code plus optional structural details, and can
never pass policy fields such as ``category`` or ``repairable``. Unregistered
codes fail fast, and anything that never became a :class:`PipelineError` is
classified ``UNCLASSIFIED`` and is therefore never repairable and never
retryable (fail closed).

This module deliberately imports nothing from the project: ``contracts.py``
depends on it, and the standalone collector template must stay independent of
it.
"""

from dataclasses import dataclass
from enum import Enum

__all__ = ['Category', 'Classification', 'ErrorSpec', 'PipelineError', 'SPECS', 'classify']

# Unknown detail keys and non-scalar values are programming errors at the call
# site, so they fail fast. Overlong strings are data-dependent, so they are
# truncated instead: a diagnostic must never replace the error it describes.
MAX_DETAIL_VALUE_LENGTH = 200


class Category(str, Enum):
    SECURITY = 'SECURITY'
    OBSERVATION = 'OBSERVATION'
    PLAN_SEMANTIC = 'PLAN_SEMANTIC'
    DATA_INTEGRITY = 'DATA_INTEGRITY'
    TRANSPORT = 'TRANSPORT'
    MODEL_OUTPUT = 'MODEL_OUTPUT'
    BUDGET = 'BUDGET'
    UNCLASSIFIED = 'UNCLASSIFIED'


@dataclass(frozen=True)
class ErrorSpec:
    """Stable classification for one error code; policy lives here, not at call sites."""

    category: Category
    repairable: bool
    retryable: bool
    phase: str
    summary: str
    detail_keys: frozenset


def _group(category, entries, *, repairable=False, retryable=False, phase='executing'):
    return {code: ErrorSpec(category=category, repairable=repairable, retryable=retryable,
                            phase=phase, summary=summary, detail_keys=frozenset(detail_keys))
            for code, summary, detail_keys in entries}


_GROUPS = (
    # --- 目标/凭证/范围越界：永远禁止 repair 与 retry ---------------------
    _group(Category.SECURITY, (
        ('invalid_url', 'Imported URL is not an acceptable target.', ()),
        ('unsupported_query', 'Imported URL query is duplicated or sensitive.', ()),
        ('entry_url_query_not_supported', 'Entry URL must not carry a query string.', ()),
        ('unsupported_shell_syntax', 'Copied command contains shell syntax.', ()),
        ('unsupported_option_or_multiple_urls', 'cURL option or extra URL is not supported.', ()),
        ('multiple_urls', 'More than one target URL was supplied.', ()),
        ('invalid_header', 'Header is malformed.', ()),
        ('expected_curl', 'Input does not start with curl.', ()),
        ('missing_url', 'No target URL supplied.', ()),
        ('missing_argument', 'A cURL flag is missing its value.', ()),
        ('curl_too_large', 'Copied command exceeds the size limit.', ()),
    ), phase='observing'),
    _group(Category.SECURITY, (
        ('only_literal_loopback_http_supported', 'Target is not a literal loopback HTTP origin.', ()),
        ('sensitive_request_not_supported', 'Observed request carries sensitive query keys.', ()),
        ('invalid_or_sensitive_field', 'Requested field is malformed or sensitive.', ('field',)),
    )),
    _group(Category.SECURITY, (
        ('sensitive_pointer', 'Plan pointer matches a sensitive key.', ('pointer',)),
    ), phase='analyzing'),
    # --- 观察阶段未取得可用证据：需要重新观察，不是改计划 ----------------
    _group(Category.OBSERVATION, (
        ('no_usable_json_requests', 'No usable JSON request was observed.', ()),
        ('expected_json', 'Imported response is not JSON.', ()),
        ('expected_object_or_list', 'Imported JSON body is not an object or list.', ()),
    ), phase='observing'),
    # --- 计划与已观察证据不符：未来唯一"改计划可能有效"的类 --------------
    _group(Category.PLAN_SEMANTIC, (
        ('unknown_request_id', 'Plan references an unobserved request id.', ('request_id',)),
        ('unobserved_page_parameter', 'Page parameter was not observed.', ('parameter',)),
        ('unique_key_not_in_fields', 'Unique key is not one of the requested fields.', ('unique_key',)),
        ('invalid_json_pointer', 'Pointer is not a valid JSON Pointer.', ('pointer',)),
        ('invalid_array_index', 'Pointer uses an invalid array index.', ('pointer',)),
        ('pointer_not_found', 'Pointer does not resolve in the observed sample.', ('pointer',)),
        ('items_not_list', 'Pointer does not resolve to an item list.', ('pointer', 'page')),
        ('invalid_total_pointer', 'Total pointer does not resolve to an integer.', ('pointer',)),
        ('invalid_has_next_pointer', 'Next-page pointer does not resolve to a boolean.', ('pointer',)),
        ('requested_fields_mismatch', 'Plan fields do not match the requested fields.', ()),
    ), repairable=True, phase='analyzing'),
    # --- 执行中数据自相矛盾/漂移：必须诚实失败 ---------------------------
    _group(Category.DATA_INTEGRITY, (
        ('no_list_sample', 'Legacy combined code; should be split into items_not_list / empty_list_sample.', ('pointer',)),
        ('empty_list_sample', 'First page item list is genuinely empty.', ('pointer',)),
        ('invalid_total', 'Total value is not a usable integer.', ('page',)),
        ('total_changed', 'Total changed between pages.', ('page',)),
        ('field_type_changed', 'Field type changed between pages.', ('page', 'field')),
        ('non_finite_number', 'Number is not finite.', ('page', 'field')),
        ('invalid_unique_key', 'Unique key is not a non-empty scalar.', ('page', 'unique_key')),
        ('duplicate_id', 'Unique key repeated across pages.', ('page', 'unique_key')),
        ('too_many_items', 'Collected more records than the declared total.', ('page',)),
        ('incomplete_items', 'Collected fewer records than the declared total.', ('page',)),
        ('invalid_has_next', 'Next-page flag is not a boolean.', ('page',)),
        ('empty_page_with_next', 'Empty page still reports a next page.', ('page',)),
    )),
    # --- 网络/HTTP 层：重试是网关职责，不是 repair -----------------------
    _group(Category.TRANSPORT, (
        ('connect_timeout', 'Connection attempt timed out.', ()),
        ('network_error', 'Transient network failure.', ()),
    ), retryable=True),
    _group(Category.TRANSPORT, (
        ('read_timeout', 'Reading the response timed out.', ()),
        ('write_timeout', 'Sending the request timed out.', ()),
        ('pool_timeout', 'Connection pool wait timed out.', ()),
        ('timeout', 'Request exceeded its total deadline.', ()),
        ('provider_error', 'Model provider returned a server error.', ()),
        ('request_rejected', 'Request was rejected by the peer.', ()),
        ('redirect_rejected', 'Peer attempted a redirect, which is not followed.', ()),
        ('response_too_large', 'Response exceeded the byte limit.', ()),
    )),
    # --- 模型输出不可解析：重新生成可能有效 ------------------------------
    _group(Category.MODEL_OUTPUT, (
        ('invalid_output', 'Model output did not match the schema.', ()),
        ('output_incomplete', 'Model output was truncated or filtered.', ()),
    ), repairable=True, phase='analyzing'),
    # --- 调用预算与供应商凭证/配额配置 -----------------------------------
    _group(Category.BUDGET, (
        ('call_budget_exceeded', 'Per-task model call budget is exhausted.', ()),
        ('input_too_large', 'Model request exceeds the byte limit.', ()),
        ('invalid_input', 'Model request could not be serialized.', ()),
        ('authentication_failed', 'Provider rejected the credential.', ()),
        ('access_denied', 'Provider denied access.', ()),
        ('rate_limited', 'Provider rate limit was hit.', ()),
        ('max_pages_out_of_range', 'Page budget is outside the allowed range.', ()),
    ), phase='analyzing'),
)


def _registry(groups):
    specs = {}
    for group in groups:
        for code, spec in group.items():
            if code in specs:
                raise ValueError(f'duplicate_error_code:{code}')
            specs[code] = spec
    return specs


SPECS = _registry(_GROUPS)


def _validated_details(spec, details):
    if details is None:
        return {}
    if not isinstance(details, dict):
        raise ValueError('details_must_be_a_mapping')
    if not set(details) <= spec.detail_keys:
        raise ValueError('detail_key_not_allowed')
    validated = {}
    for key, value in details.items():
        if type(value) is bool or not isinstance(value, (int, str)):
            raise ValueError('detail_value_not_structural')
        if isinstance(value, str) and len(value) > MAX_DETAIL_VALUE_LENGTH:
            value = value[:MAX_DETAIL_VALUE_LENGTH]
        validated[key] = value
    return validated


class PipelineError(ValueError):
    """Pipeline failure carrying a stable code and registry-derived policy.

    ``str(error)`` is exactly the code, so existing code and tests that compare
    the message keep working; structural context lives in ``details`` instead.
    """

    def __init__(self, code, details=None):
        spec = SPECS.get(code)
        if spec is None:
            raise KeyError(f'unknown_error_code:{code}')
        super().__init__(code)
        self.code = code
        self.category = spec.category
        self.repairable = spec.repairable
        self.retryable = spec.retryable
        self.phase = spec.phase
        self.detail_keys = spec.detail_keys
        self.details = _validated_details(spec, details)


@dataclass(frozen=True)
class Classification:
    code: str
    category: Category
    repairable: bool
    retryable: bool


def classify(error):
    """Classify any exception without ever parsing its message.

    Only a machine-readable ``code`` attribute is trusted; everything else,
    including plain ``ValueError``, is fail-closed as ``UNCLASSIFIED``.
    """
    code = getattr(error, 'code', None)
    if not isinstance(code, str):
        code = type(error).__name__
    spec = SPECS.get(code)
    if spec is None:
        return Classification(code, Category.UNCLASSIFIED, False, False)
    return Classification(code, spec.category, spec.repairable, spec.retryable)
