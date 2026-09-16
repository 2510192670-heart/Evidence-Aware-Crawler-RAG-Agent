"""M7-A S3.2-A: TargetPolicy enforcement 层。

为 runtime 接线（S3.2-B：main / observe / curl_import / contracts）提供稳定入口。
判定本体不在这里：loopback 与 public_http 共用 ``policy.target.check_target``——
全系统只允许一套目标判定标准，本模块不引入第二套词汇，也不复制任何规则。

边界（S3.2-A）：
- 纯判定，无 IO：不做 DNS 解析、不发请求、不触达 Playwright；
- SSRF admission（解析 + IP 分类）属于 S3.3，届时以 ``admit_target()``  additive
  进入本模块，``check_target`` 契约不变。

loopback 语义说明：委托 ``local_origin`` 意味着只允许**字面量** 127.0.0.1/::1，
``localhost`` 主机名被拒绝（DNS 歧义 → fail-closed）。该行为由 S2 快照
tests/snapshots/m7_policy/loopback_behavior.json 钉住，enforcement 层不得放宽。
"""

from .errors import TargetPolicyError
from .target import TargetPolicy
from .target import check_target as _check_target

__all__ = ['TargetPolicyError', 'check_target']


def check_target(url: str, policy: TargetPolicy | None = None) -> str:
    """enforcement 入口：允许 → 返回规范化 origin；拒绝 → 稳定码异常。

    - loopback policy（含 policy=None）：逐字节委托 local_origin——允许字面量
      127.0.0.1/::1，拒绝 localhost、公网与其他一切目标；拒绝保持裸
      ValueError('only_literal_loopback_http_supported')，不升级为
      TargetPolicyError（S2 快照钉住的等价性契约）；
    - public_http policy：按 allowlist 域名做点边界匹配；未命中或
      scheme/端口/URL 形态非法时抛 TargetPolicyError（str(error) 恰为码）。
    """
    return _check_target(url, policy)
