"""M7-A S3.3-C1: 部署级策略装载闸。

S3.3 唯一允许构造 public_http 策略的入口：strict JSON 文件 → TargetPolicy →
公网基础约束复查。本模块不扩展 TargetPolicy 字段（C1 约束），只拒绝不放宽：
- public 策略必须 https-only、allowlist 非空（后者由模型契约保证）；
- 任何非法文件/非法形态收敛为两个稳定码，不回显文件内容（防泄漏）。
"""

import json
from pathlib import Path

from pydantic import ValidationError

from .errors import TargetPolicyError
from .target import TargetPolicy

__all__ = ['load_policy_file']

MAX_POLICY_FILE_BYTES = 64 * 1024


def load_policy_file(path) -> TargetPolicy:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise TargetPolicyError('policy_file_not_readable') from None
    if len(raw) > MAX_POLICY_FILE_BYTES:
        raise TargetPolicyError('policy_file_not_readable')
    try:
        document = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise TargetPolicyError('invalid_policy_file') from None
    if not isinstance(document, dict):
        raise TargetPolicyError('invalid_policy_file')
    try:
        policy = TargetPolicy(**document)
    except ValidationError:
        raise TargetPolicyError('invalid_policy') from None
    if policy.mode == 'public_http' and policy.schemes != ('https',):
        # S3.3 公网只开放 https；http 支持需后续里程碑显式批准。
        raise TargetPolicyError('invalid_policy')
    return policy
