"""仅从显式环境变量读取配置，不自动寻找其他项目的凭证。"""

from dataclasses import dataclass, field
import math
import os
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CloudConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    response_mode: str = 'json_object'
    output_token_field: str = 'max_tokens'
    timeout_seconds: float = 120
    max_output_tokens: int = 1000
    thinking: str | None = None

    def __post_init__(self):
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme == 'https' and bool(url.hostname) and not url.username
                     and not url.password and not url.query and not url.fragment)
            _ = url.port
        except (ValueError, TypeError):
            valid = False
        if not valid or any(c.isspace() for c in self.base_url):
            raise ValueError('WDA_LLM_BASE_URL 必须为不含凭证、查询或片段的 HTTPS API 根地址')
        if not self.api_key.strip() or any(c in self.api_key for c in '\r\n'):
            raise ValueError('WDA_LLM_API_KEY 无效')
        if not self.model.strip():
            raise ValueError('WDA_LLM_MODEL 不能为空')
        if self.response_mode not in {'json_object', 'json_schema', 'none'}:
            raise ValueError('WDA_LLM_RESPONSE_MODE 无效')
        if self.output_token_field not in {'max_tokens', 'max_completion_tokens'}:
            raise ValueError('WDA_LLM_OUTPUT_TOKEN_FIELD 无效')
        if self.thinking not in {None, 'disabled', 'enabled'}:
            raise ValueError('WDA_LLM_THINKING 无效')
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120:
            raise ValueError('WDA_LLM_TIMEOUT_SECONDS 应大于 0 且不超过 120')
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 1000:
            raise ValueError('WDA_LLM_MAX_OUTPUT_TOKENS 应为 1～1000 的整数')
        object.__setattr__(self, 'base_url', self.base_url.rstrip('/'))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        source = os.environ if env is None else env
        required = ['WDA_LLM_BASE_URL', 'WDA_LLM_API_KEY', 'WDA_LLM_MODEL']
        if any(not source.get(name, '').strip() for name in required):
            raise ValueError('请配置 WDA_LLM_BASE_URL、WDA_LLM_API_KEY、WDA_LLM_MODEL')
        try:
            timeout = float(source.get('WDA_LLM_TIMEOUT_SECONDS', '120'))
            tokens = int(source.get('WDA_LLM_MAX_OUTPUT_TOKENS', '1000'))
        except ValueError:
            raise ValueError('模型超时或输出 token 配置不是有效数字') from None
        return cls(
            base_url=source['WDA_LLM_BASE_URL'], api_key=source['WDA_LLM_API_KEY'],
            model=source['WDA_LLM_MODEL'],
            response_mode=source.get('WDA_LLM_RESPONSE_MODE', 'json_object'),
            output_token_field=source.get('WDA_LLM_OUTPUT_TOKEN_FIELD', 'max_tokens'),
            timeout_seconds=timeout, max_output_tokens=tokens,
            thinking=source.get('WDA_LLM_THINKING') or None,
        )

    @classmethod
    def deepseek_from_env(cls, env: Mapping[str, str] | None = None):
        """用户选定的 DeepSeek Flash 预设；不会发起网络请求。"""
        values = {
            'WDA_LLM_BASE_URL': 'https://api.deepseek.com',
            'WDA_LLM_MODEL': 'deepseek-flash',
            'WDA_LLM_RESPONSE_MODE': 'json_object',
            'WDA_LLM_OUTPUT_TOKEN_FIELD': 'max_tokens',
            'WDA_LLM_THINKING': 'disabled',
        }
        values.update(os.environ if env is None else env)
        if not values.get('WDA_LLM_API_KEY', '').strip():
            values['WDA_LLM_API_KEY'] = values.get('DEEPSEEK_API_KEY', '')
        return cls.from_env(values)
