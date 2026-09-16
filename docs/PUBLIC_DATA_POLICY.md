# Public Data Policy（公开数据采集政策）

状态：M7 Governance Layer 治理文档。本政策描述系统的数据采集边界，
由 `backend/app/policy/data_policy.py`（DataPolicy）以确定性规则执行。

本文件描述运行与支持范围，不限制 MIT 对软件副本的授权。关键词分类不是
法律裁定或完整内容审查；`PUBLIC` 标签不能证明数据不含个人信息或已取得
权利许可。当前部署与法律审查说明见 [中国法律合规指南](COMPLIANCE_CN.md)。

---

## 1. 系统用途

Web Data Agent 是面向公开网站的 AI 辅助数据采集工作台，仅限内部使用：

- 公开网络数据研究（purpose: `research`）
- 公开信息整理与归档（purpose: `analysis` / `archiving`）
- 数据分析实验（purpose: `analysis`）
- 公开页面的有限监控（purpose: `monitoring`）

本系统不是第三方爬虫平台，不开放任意用户使用，不含账号体系与商业化能力。

## 2. 数据分类（DataClassification）

| 分类 | 默认处置 | 说明 |
| --- | --- | --- |
| `PUBLIC` | 允许 | 公开可访问、非个人、非受限数据 |
| `AUTHORIZED` | 需要明确授权 | v1 无授权通道，fail-closed 拒绝 |
| `SENSITIVE` | 拒绝 | 个人身份信息、隐私数据 |
| `RESTRICTED` | 拒绝 | 绕过/破解类意图 |

## 3. 禁止事项

以下意图在任务准入（admission）阶段被确定性拒绝，不落库、零 IO：

**个人身份信息（reason: `possible_personal_data_collection`）**

- 手机号、电话号码、身份证、银行卡
- 私人地址、家庭住址、个人信息、个人身份

**隐私数据（reason: `privacy_data_collection`）**

- 用户列表、私人账号、个人隐私、隐私数据
- 聊天记录、通讯录

**绕过行为（reason: `circumvention_not_allowed`）**

- 破解、绕过（含绕过登录/权限/验证）、验证码处理
- cookie 盗取/窃取、突破网站限制
- 任何规避网站访问控制或反爬机制的能力

对应系统级原则：

1. 只访问公开可访问资源
2. 不绕过登录、权限控制、验证码
3. 不采集个人敏感数据
4. 不提供规避网站限制能力

## 4. 任务用途声明（purpose）

- 任务可携带 `purpose` 字段（封闭词汇：`research` / `analysis` /
  `monitoring` / `archiving` / `other`），词汇外取值以
  `unsupported_purpose` 拒绝（API 层 `invalid_request`）。
- `purpose` 与 DataPolicy 治理决策一并持久化：
  - `tasks.spec` JSON 列（`data_policy` 键，含分类/allowed/reason/matched_rule）
  - `report.json` artifact（`purpose` additive 键）
- 治理决策可追溯，不只在内存判断。

## 5. 拒绝响应契约

所有 DataPolicy 拒绝共用一个稳定错误码，`reason` 区分成因：

```json
{
  "code": "data_policy_restricted",
  "message": "data_policy_restricted",
  "reason": "possible_personal_data_collection",
  "task_id": null,
  "retryable": false
}
```

reason 封闭词汇：`possible_personal_data_collection` /
`privacy_data_collection` / `circumvention_not_allowed` /
`authorization_required` / `restricted_data_not_supported`。

决策记录只含规则 id，绝不回显用户原始描述或命中关键词（防泄漏）。

## 6. 边界与限制

- v1 为关键词 + 分类的确定性规则层，不调用大模型；规则是保守下界，
  不构成对未列举意图的许可——未列举意图仍受 TargetPolicy /
  RuntimePolicy 与人工审查约束。
- DataPolicy 只回答"是否应该采集这种数据"，不回答"是否允许访问这个
  目标"（TargetPolicy）与"如何安全执行"（RuntimePolicy）。
- 规则集扩展需评审：放宽任何拒绝规则视为治理边界变更。
