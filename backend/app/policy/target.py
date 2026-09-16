"""M7-A S1: TargetPolicy 基础层——目标判定的单一真源。

设计边界：
- 本模块只消费 ``pipeline.contracts.local_origin``，不依赖 ``pipeline.errors``
  （错误契约见 ``policy/errors.py``），不被任何现有模块反向依赖
  （storage/rag/evaluation/export 均不 import 本模块），也不修改任何冻结文件。
- loopback 模式逐字节委托现有 ``local_origin``（含其裸 ValueError 行为），
  ``policy=None`` 的既有调用方零行为漂移。
- public_http 模式只做确定性判定（allowlist/scheme/端口/query），不发请求、
  不做 DNS 解析；SSRF IP-pin、robots、限速属于后续切片（S3）。
"""

import hashlib
import ipaddress
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

from ..pipeline.contracts import local_origin
from .errors import TargetPolicyError

__all__ = ['AllowRule', 'TargetPolicy', 'check_target', 'default_policy', 'policy_sha256']

_DOMAIN_LABEL = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?')
_SCHEME_DEFAULT_PORTS = {'https': 443, 'http': 80}


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _valid_domain(value: str) -> bool:
    """白名单/目标域名的确定性形态校验：小写、点分标签、长度受限。

    单标签主机名（如 localhost）在公网模式有解析歧义，fail-closed 拒绝；
    域名必须已 IDNA 编码（punycode），原始 Unicode 会被字符集校验拒绝。
    """
    if not value or len(value) > 253 or value != value.lower():
        return False
    labels = value.split('.')
    if len(labels) < 2:
        return False
    return all(len(label) <= 63 and _DOMAIN_LABEL.fullmatch(label) for label in labels)


class AllowRule(BaseModel):
    """域名白名单条目：点边界匹配；IP 字面量不可入册，从根上消除编码绕过面。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    domain: str
    include_subdomains: bool = False

    @model_validator(mode='after')
    def check_domain(self):
        if not _valid_domain(self.domain) or _is_ip_literal(self.domain):
            raise ValueError('invalid_allowlist_domain')
        return self


class TargetPolicy(BaseModel):
    """内容寻址的目标策略（frozen + extra=forbid）。

    S1 仅承载 check_target 消费的字段；限速/robots/重定向等执行期字段属于
    后续切片，届时以 additive 字段扩展。
    """

    model_config = ConfigDict(frozen=True, extra='forbid')
    schema_version: Literal[1] = 1
    mode: Literal['loopback', 'public_http'] = 'loopback'
    target_kind: Literal['http', 'browser'] = 'http'
    allowlist: tuple[AllowRule, ...] = ()
    schemes: tuple[Literal['https', 'http'], ...] = ('https',)
    entry_url_query_allowed: bool = False

    @model_validator(mode='after')
    def check_mode_fields(self):
        if self.mode == 'loopback':
            if self.allowlist:
                # loopback 语义完全由 local_origin 决定，携带 allowlist 是配置错误。
                raise ValueError('invalid_policy')
        elif not self.allowlist or not self.schemes:
            raise ValueError('invalid_policy')
        return self


def default_policy() -> TargetPolicy:
    return TargetPolicy()


def policy_sha256(policy: TargetPolicy) -> str:
    """策略的内容寻址哈希，与 plan_hash 同一套规范化规则。"""
    canonical = json.dumps(policy.model_dump(mode='json'), sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _public_origin(url: str, policy: TargetPolicy) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        # 非法端口等解析错误统一收敛为 invalid_url，不回显原始输入。
        raise TargetPolicyError('invalid_url') from None
    if parsed.username or parsed.password or parsed.fragment or not host:
        raise TargetPolicyError('invalid_url')
    if parsed.scheme not in policy.schemes:
        raise TargetPolicyError('public_scheme_not_allowed')
    if port is not None and port != _SCHEME_DEFAULT_PORTS[parsed.scheme]:
        raise TargetPolicyError('unsupported_port')
    if parsed.query and not policy.entry_url_query_allowed:
        raise TargetPolicyError('entry_url_query_not_supported')
    if _is_ip_literal(host) or not _valid_domain(host):
        # IP 字面量主机（含十进制/IPv6 形态）不参与 allowlist 匹配，fail-closed。
        raise TargetPolicyError('target_not_in_allowlist')
    for rule in policy.allowlist:
        if host == rule.domain or (rule.include_subdomains and host.endswith('.' + rule.domain)):
            return f'{parsed.scheme}://{host}'
    raise TargetPolicyError('target_not_in_allowlist')


def check_target(url: str, policy: TargetPolicy | None = None) -> str:
    """判定 URL 是否为允许的目标，返回规范化 origin。

    loopback 模式（含 policy=None）逐字节委托现有 local_origin，异常类型与
    错误码与现状完全一致；public_http 模式抛带注册码的 PipelineError。
    """
    if policy is None:
        policy = default_policy()
    if policy.mode == 'loopback':
        return local_origin(url)
    return _public_origin(url, policy)
