# Security Boundary（三层策略安全边界）

状态：M7 Governance Layer 治理文档。描述系统准入链上的三层策略分工与
各自的安全边界。

---

## 1. 三层策略架构

```
UserIntent（description / purpose / fields）
   ↓
DataPolicy        —— 是否应该采集这种数据（数据边界）
   ↓
TargetPolicy      —— 是否允许访问这个目标（目标边界）
   ↓
RuntimePolicy     —— 如何安全执行（执行边界）
   ↓
Collector（确定性执行产物）
```

三层职责严格分离，互不替代、互不复制规则：

| 层 | 模块 | 回答的问题 | 拒绝形态 |
| --- | --- | --- | --- |
| DataPolicy | `backend/app/policy/data_policy.py` | 是否应该采集这种数据 | `data_policy_restricted` + reason |
| TargetPolicy | `backend/app/policy/target.py`（enforcement 为唯一入口） | 是否允许访问这个目标 | `target_not_in_allowlist` 等稳定码 |
| RuntimePolicy | `policy/transport.py`、`policy/robots.py`、`policy/resolution.py`、`run_task` guard | 如何安全执行 | `ssrf_ip_blocked`、`robots_disallowed`、`public_mode_not_enabled` 等 |

## 2. DataPolicy（M7 Governance Layer 新增）

- 纯确定性：关键词 + 分类，不调用 LLM、无 IO；
- 判定对象是用户意图（description / purpose / fields），不看 URL；
- 默认规则：`PUBLIC` 允许；`AUTHORIZED` 需显式授权（v1 fail-closed）；
  `SENSITIVE` / `RESTRICTED` 拒绝；
- 拒绝发生在 `TaskService.submit()` 准入最前端：不落库、零 IO；
- 决策（分类 / allowed / reason / matched_rule）随 `tasks.spec` 持久化，
  `purpose` 同时进入 `report.json`，形成可追溯审计链；
- 错误契约为 standalone `DataPolicyError`（`str(error)` 恰为稳定码），
  不修改冻结的 `pipeline/errors.py` 注册表。

数据边界详见 `docs/PUBLIC_DATA_POLICY.md`。

## 3. TargetPolicy

- loopback 模式：只允许字面量 `127.0.0.1` / `::1` HTTP（`localhost`
  因 DNS 歧义 fail-closed 拒绝），行为由 S2 等价性快照钉住；
- public_http 模式：https-only、域名 allowlist 点边界匹配、IP 字面量
  全拒、非默认端口拒绝、入口 query 受控；
- 策略只能来自部署侧可信注册表（`WDA_TARGET_POLICY_FILE` →
  `load_policy_file`），请求侧只能携带内容寻址 sha256 引用，
  不能内联策略对象；
- `enforcement.check_target` 是唯一权威目标判定入口，全系统不存在
  第二套目标判定词汇。

## 4. RuntimePolicy（执行期安全）

- SSRF resolution guard：DNS 解析后 IP 分类，私网/保留段拒绝
  （`ssrf_ip_blocked`），pinning 消除 check 与 fetch 之间的 rebinding 窗口；
- robots policy：路径级 robots 规则，读取失败 fail-closed
  （`robots_disallowed`）；
- public transport：受控 HTTP 传输（超时/字节上限/不跟随重定向越权）；
- `run_task` guard：运行时策略未开放的形态在任何观察/网络 IO 之前
  fail-closed；
- JSON GET/POST 执行只允许变更已观察的页码字段；其余 URL/query/method/
  请求体是证据派生的不可变契约。
- HTML 模式可跟随实际页面中观察到的列表分页和详情链接，每个目标重新经过
  同源与目标检查并受请求预算限制；不受“固定 JSON 请求仅变页码”的规则描述。

## 5. 全局不变量

- 三层策略全部是确定性代码：LLM 只提出计划，永不执行任意生成代码；
- 所有拒绝码稳定、fail-closed：未注册码在 `classify()` 中归为
  UNCLASSIFIED，不可 repair、不可 retry；
- 错误与决策记录不回显用户输入、URL、响应正文或命中关键词（防泄漏）；
- SECURITY 类失败永不自动修复；repair 边界（`pointer_not_found` 自动、
  `page_location_mismatch` 仅提案）不受治理层影响；
- 治理层放宽（新增允许规则、缩小拒绝词汇）视为安全边界变更，需要
  显式评审与回归验证。
