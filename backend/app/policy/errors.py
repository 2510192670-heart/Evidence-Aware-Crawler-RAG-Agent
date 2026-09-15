"""M7-A TargetPolicy 错误契约（独立于冻结的 pipeline.errors）。

设计边界：
- backend/app/pipeline 受 v0.5.4-freeze 字节级冻结契约保护
  （tests/test_m55_failure_fixtures.py::test_frozen_compatibility_and_viewer_contract），
  errors.SPECS 不能在 S1 扩展，因此本模块不依赖、也不修改 pipeline.errors。
- TargetPolicyError 与 PipelineError 同约定：``str(error)`` 恰为稳定错误码，
  异常消息绝不携带 URL、响应值等上下文（防泄漏，与 failure_fields 原则一致）。
- 新码（target_not_in_allowlist / public_scheme_not_allowed / unsupported_port）
  暂未注册进 SPECS，pipeline 的 ``classify()`` 对其 fail-closed 为 UNCLASSIFIED
  （不可 repair、不可 retry）；复用的既有码（invalid_url /
  entry_url_query_not_supported）经 ``classify()`` 照常归入 SECURITY。
- S3 接入管线（需里程碑批准解冻 backend）时统一迁移为 PipelineError。
"""

__all__ = ['TargetPolicyError']


class TargetPolicyError(ValueError):
    """公网目标拒绝；``code`` 属性与 ``str(error)`` 为同一稳定错误码。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code
