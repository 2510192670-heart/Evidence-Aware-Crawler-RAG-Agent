"""M7 Governance Layer: DataPolicy——内部使用数据边界判定。

职责分离（三层策略，见 docs/SECURITY_BOUNDARY.md）：
- DataPolicy（本模块）：是否**应该**采集这种数据（意图/内容边界）；
- TargetPolicy（policy/target.py）：是否**允许**访问这个目标；
- RuntimePolicy（enforcement/transport/robots/resolution + run_task guard）：
  如何**安全执行**。

设计边界：
- 纯确定性判定：关键词 + 分类，不调用 LLM、无 IO、不触达网络；
- 不依赖也不修改 pipeline/errors.py：沿用 M7-A standalone 错误契约
  （DataPolicyError，``str(error)`` 恰为稳定码），admission 阶段直接 422，
  未注册码在 classify() 中 fail-closed 为 UNCLASSIFIED；
- 不替代 TargetPolicy：本模块不看 URL、不看 allowlist，只看用户意图
  （description / purpose / fields）；
- fail-closed：默认放行仅限 PUBLIC；AUTHORIZED 需要显式授权；
  SENSITIVE / RESTRICTED 一律拒绝；
- 防泄漏：决策只记录规则 id，绝不回显命中的关键词或原始描述。
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .errors import DataPolicyError

__all__ = ['DataClassification', 'DataDecision', 'PURPOSES', 'admit_intent',
           'decide', 'evaluate_intent']


class DataClassification(str, Enum):
    """数据分类封闭词汇：默认规则允许 PUBLIC，其余 fail-closed。"""

    PUBLIC = 'PUBLIC'
    AUTHORIZED = 'AUTHORIZED'
    SENSITIVE = 'SENSITIVE'
    RESTRICTED = 'RESTRICTED'


# 任务用途封闭词汇（TaskInput 与本模块共用同一真源）。
PURPOSES = frozenset({'research', 'analysis', 'monitoring', 'archiving', 'other'})

# 拒绝码是稳定契约：所有 DataPolicy 拒绝共用一个 code，reason 区分成因。
DENIAL_CODE = 'data_policy_restricted'

# 规则按声明顺序优先：RESTRICTED（绕过行为）先于 SENSITIVE（个人/隐私数据）。
# 关键词全部小写；中文不受大小写影响，英文命中靠 text.lower() 归一。
_FORBIDDEN_RULES = (
    ('circumvention', DataClassification.RESTRICTED, 'circumvention_not_allowed', (
        '破解', '绕过', '验证码', '突破限制', 'cookie盗取', 'cookie窃取', '窃取cookie',
        'bypass', 'crack', 'captcha', 'circumvent', 'anti-bot', 'steal cookie',
    )),
    ('personal_identity', DataClassification.SENSITIVE, 'possible_personal_data_collection', (
        '手机号', '电话号码', '身份证', '银行卡', '私人地址', '家庭住址', '个人信息', '个人身份',
        'phone number', 'id card', 'identity card', 'bank card', 'social security',
        'personal address', 'home address', 'personal information',
    )),
    ('privacy', DataClassification.SENSITIVE, 'privacy_data_collection', (
        '用户列表', '私人账号', '个人隐私', '隐私数据', '聊天记录', '通讯录',
        'user list', 'private account', 'personal data', 'private data',
        'chat history', 'contact list',
    )),
)


class DataDecision(BaseModel):
    """治理决策记录（frozen + extra=forbid），随 tasks.spec 持久化形成审计链。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    schema_version: Literal[1] = 1
    classification: DataClassification
    allowed: bool
    code: str | None = None
    reason: str | None = None
    purpose: str | None = None
    matched_rule: str | None = None


def decide(classification: DataClassification, *, authorized: bool = False):
    """分类 → (allowed, reason)：默认规则的唯一真源。

    PUBLIC 允许；AUTHORIZED 需要显式授权（v1 无授权通道，fail-closed 拒绝）；
    SENSITIVE / RESTRICTED 一律拒绝。
    """
    if classification is DataClassification.PUBLIC:
        return True, None
    if classification is DataClassification.AUTHORIZED:
        return authorized, None if authorized else 'authorization_required'
    return False, 'possible_personal_data_collection' if classification is DataClassification.SENSITIVE \
        else 'restricted_data_not_supported'


def evaluate_intent(description='', purpose=None, fields=(), *, authorized=False) -> DataDecision:
    """对用户意图做确定性数据分类，返回治理决策（不抛拒绝异常）。

    description / fields 是用户数据：只做小写归一后的子串匹配，
    命中内容不进入决策记录（只留规则 id）。
    """
    if purpose is not None and purpose not in PURPOSES:
        raise ValueError('unsupported_purpose')
    text = ' '.join([description, *(str(name) for name in fields)]).lower()
    classification, reason, matched_rule = DataClassification.PUBLIC, None, None
    for rule_id, rule_classification, rule_reason, keywords in _FORBIDDEN_RULES:
        if any(keyword in text for keyword in keywords):
            classification, reason, matched_rule = rule_classification, rule_reason, rule_id
            break
    allowed, decide_reason = decide(classification, authorized=authorized)
    if reason is None:
        reason = decide_reason
    return DataDecision(classification=classification, allowed=allowed,
                        code=None if allowed else DENIAL_CODE,
                        reason=None if allowed else reason,
                        purpose=purpose, matched_rule=matched_rule)


def admit_intent(description='', purpose=None, fields=(), *, authorized=False) -> DataDecision:
    """admission 入口：允许 → 返回决策；拒绝 → 抛 DataPolicyError（稳定码 + reason）。"""
    decision = evaluate_intent(description, purpose, fields, authorized=authorized)
    if not decision.allowed:
        raise DataPolicyError(decision.code, decision.reason)
    return decision
