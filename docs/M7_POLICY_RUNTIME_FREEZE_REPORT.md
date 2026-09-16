# M7-A Policy Runtime Freeze Report

> 历史阶段快照：下文“当前”指本报告最初记录的节点。后续公网运行链路已扩展，现状见 [工作台指南](WORKBENCH_V1.md)；保留原文供追溯，不作为当前功能限制或本次发布声明。

状态：policy runtime 阶段收口文档。本文档创建轮次不含任何代码/测试修改，
未 commit、未 push、未 tag（tag 等待人工确认）。

---

## 1. Freeze Scope

**阶段目标**：为系统引入 TargetPolicy 层，在保留 loopback 安全模型
（`local_origin` 语义零改动）的前提下，为公网目标（public_http）建立
服务端可信的策略准入链。

**当前支持范围（本冻结节点已完成）**：

- TargetPolicy / AllowRule 契约（frozen pydantic、extra=forbid、内容寻址 sha256）
- loopback 模式：逐字节委托现有 `local_origin`（含裸 ValueError 行为）
- public_http 模式判定：https-only、域名 allowlist（点边界）、端口/形态校验
- policy admission：`POST /api/v1/tasks` 经 `resolve_policy` →
  `enforcement.check_target` 后才落库
- run_task policy guard：非 loopback 策略在观察之前 fail-closed
- S2 等价性快照基线（loopback 行为钉住）

**当前未开放范围（明确不在本节点内）**：

- **公网执行尚未开放**：policy admission 已完成，但携带 public_http 策略的
  任务在 `run_task` 守卫处以 `public_mode_not_enabled` 诚实失败，零 IO。
- observe / curl_import / execution / contracts 尚未接入 enforcement
  （仍为 loopback-only 原行为）。
- 公网目标执行（public target execution）自 **S3.3** 起才进入实施。

---

## 2. Delivery Timeline

### S1 — `925e220`

`feat(policy): add M7-A S1 TargetPolicy base layer with standalone error contract`

- `TargetPolicy`（mode/target_kind/allowlist/schemes/entry_url_query_allowed，
  frozen + extra=forbid，默认 loopback）
- `AllowRule`（域名形态校验；IP 字面量不可入册）
- `TargetPolicyError`：**standalone error contract**（`code` 属性，
  `str(error)` 恰为稳定码；不依赖冻结的 `pipeline.errors` 注册表）
- `default_policy()` / `policy_sha256()`（与 `plan_hash` 同套规范化规则）
- `check_target()`：loopback 逐字节委托 `local_origin`；public 确定性判定
- 文件：`backend/app/policy/{__init__,errors,target}.py`、
  `tests/test_policy_contract.py`

### S2 — `2120438`

`test(policy): add M7-A S2 loopback equivalence snapshot baseline`

- loopback snapshot：`tests/snapshots/m7_policy/loopback_behavior.json`
  （3 accepted + 6 rejected，覆盖 localhost 与 127.0.0.1 双族）
- accepted/rejected equivalence：每条同时跑 `check_target` 与 `local_origin`，
  结果、异常类型（精确 `type is ValueError`）、错误码逐一相同
- **local_origin 行为冻结**：拒绝路径保持裸 ValueError，不升级为
  TargetPolicyError；快照即回归锚点
- 文件：`tests/test_policy_snapshot.py`

### S3 runtime completion — `b8dc88b`

`feat(policy): wire target enforcement into task admission`

- TaskInput policy admission：新增 `policy_sha256` 引用字段（默认 None =
  loopback）；`check_url` 升级为跨字段校验器——无引用时保持旧 loopback
  判定不变，有引用时仅结构预检（不复制 allowlist 规则）；内联 policy 对象
  被 `extra='forbid'` 拒绝
- service layer enforcement wiring：`resolve_policy()`（None → loopback /
  非法形态 → `invalid_policy_reference` / 未注册 → `policy_not_found`）；
  `submit()` 内调用 `enforcement.check_target(url, policy)` 作为唯一权威
  准入点；规范化策略注入 `tasks.spec` 持久化（任务级审计链）
- runtime adapter：`pipeline_worker` 从 spec 重建 `args.policy` →
  `run_task(..., policy=None)` 三级回落（kwarg > args > default loopback）
- 文件：`backend/app/main.py`、`backend/app/tasks/service.py`、
  `tests/test_policy_task_admission.py`、`tests/test_policy_runtime_adapter.py`

### S3 runtime completion — `0b4d60b`

`feat(policy): add enforcement layer and run_task policy guard`

- enforcement layer：`backend/app/policy/enforcement.py` —— 纯委托
  `policy.target.check_target`，**不复制任何规则**（全系统单一判定真源）；
  纯函数无 IO；预留 S3.3 `admit_target()`（DNS 解析 + SSRF IP 分类）位置
- run_task policy guard：`policy.mode != 'loopback'` →
  `TargetPolicyError('public_mode_not_enabled')`，位于观察阶段之前
- public target fail-closed：公网策略任务零 IO 诚实失败，失败码进入既有
  `failure_fields` 报告通道（classify 对未注册码 fail-closed 为 UNCLASSIFIED）
- 文件：`backend/app/pipeline/run.py`、`backend/app/policy/enforcement.py`、
  `tests/test_policy_enforcement.py`
- 备注：本 commit 同时闭合了 `b8dc88b` 单独 checkout 时的依赖缺口
  （service.py → enforcement.py / run_task guard），HEAD 树自洽。

---

## 3. Runtime Architecture Boundary

当前链路：

```
TaskInput (policy_sha256: 引用，禁止内联对象)
   ↓
resolve_policy (服务端可信注册表；None → loopback 默认)
   ↓
enforcement.check_target (唯一权威准入判定)
   ↓
TargetPolicy (frozen；随 tasks.spec 持久化 = 审计链)
   ↓
run_task guard (mode ≠ loopback → public_mode_not_enabled，观察前 fail-closed)
   ↓
execution (现状 loopback 原逻辑，未改动)
```

边界不变量：

- **policy 是唯一判定入口**：目标合法性只由 `TargetPolicy` +
  `check_target` 决定；API 校验器只做结构预检，不复制规则。
- **enforcement 不复制规则**：`enforcement.check_target` 是对
  `target.check_target` 的纯委托，系统内不存在第二套目标判定词汇。
- **run_task 在执行前 fail-closed**：未开放的公网模式在任何观察/网络
  IO 之前被守卫拒绝，失败进入既有分类与报告通道。

---

## 4. Security Boundary

**Loopback（保持）**：

- `localhost`：拒绝（`only_literal_loopback_http_supported`，DNS 歧义
  fail-closed，S2 快照钉住）
- `127.0.0.1` / `::1`：字面量允许（http），行为与 M7 之前逐字节一致
- 无 policy 引用的任务：与 M7 之前完全相同的判定与 422 响应

**Public target（当前状态）**：

- admission 层存在且已接线：https-only、allowlist 点边界匹配
  （`evilexample.com` / `example.com.evil.net` 类绕过拒绝）、IP 字面量
  （含十进制/十六进制/IPv6 形态）全拒、非默认端口拒、入口 query 受控
- execution 尚未开放：admission 通过后仍被 run_task 守卫拦截

**当前禁止（有代码级闸门）**：

- 未授权公网访问：未注册/未命中的策略与域名一律 422 拒绝，不落库、
  不触达 worker
- 非 allowlist target：`target_not_in_allowlist`（含策略不匹配组合）
- 未注册策略执行：`policy_not_found` / `invalid_policy_reference`；
  前端不能构造规则（只能引用部署侧 sha256）
- 内联 policy 对象提交：`extra='forbid'` → 422 `invalid_request`

---

## 5. Verification Evidence

实际执行（本文档创建轮次真实运行，非历史转录）：

```
日期: 2026-09-15 (Asia/Shanghai)
解释器: .venv\Scripts\python.exe (项目虚拟环境)
命令:
  pytest tests/test_policy_contract.py tests/test_policy_snapshot.py
         tests/test_policy_enforcement.py tests/test_policy_task_admission.py
         tests/test_policy_runtime_adapter.py -q

结果: 146 passed, 0 failed, 2 warnings in 3.45s
```

warnings 均为既有第三方弃用提示（starlette testclient / anyio 别名），
与 policy 无关，M7 之前即存在。

分项构成：contract 57 + snapshot 25 + enforcement 29 + admission 16 +
runtime adapter 19 = 146。

范围说明：本节证据覆盖 policy 测试族。全量回归
（`pytest tests -q`）在 v0.5.4 冻结测试重锚前不完整适用，见 §6.1。

---

## 6. Known Limitations

1. **v0.5.4-freeze 字节冻结不再适用于当前 runtime 演进。**
   原因：`backend/app/main.py`、`backend/app/tasks/service.py`、
   `backend/app/pipeline/run.py` 已发生批准修改（M7-A S3 范围），
   `test_frozen_compatibility_and_viewer_contract` 的逐字节比对将失败，
   直至冻结基线经人工批准重锚至新 tag（建议 `v0.7.0-policy-runtime-freeze`，
   本轮不打）。M5.5 viewer/fixture 契约本身未被本轮改动触碰。

2. **公网目标执行未完成。** 当前状态：admission ready / execution not
   enabled。公网策略任务在 run_task 守卫处以 `public_mode_not_enabled`
   诚实失败；不得将本节点解读为已具备公网采集能力。

3. **以下能力留给 S3.3**：
   - public HTTP collector（执行链接入 enforcement + 公网分页执行）
   - SSRF resolution protection（解析后 IP 分类，`ssrf_ip_blocked`）
   - DNS pinning（消除 check 与 fetch 之间的 rebinding 窗口）
   - robots policy
   - rate limit（每域令牌桶）
   - evidence artifact pipeline（policy 快照进入证据链/artifact）

4. **本文档不宣称**：
   - crawler platform completed
   - production crawling ready
   - public web execution verified

   其他如实记录：新错误码尚未注册 `errors.SPECS`（classify fail-closed 为
   UNCLASSIFIED，S3.3 翻转）；curl_import / observe / execution / contracts
   四个禁改文件零触碰；`b8dc88b` 单 commit checkout 不自洽属已知历史事实，
   由 `0b4d60b` 闭合。

---

## 7. Next Stage

仅规划，不执行。

**M7-B / S3.3：Public Target Execution Layer**

- Target Resolver：DNS 解析 + 私网/保留段 IP 分类 + pinned 连接
  （`policy/resolution.py`，resolver 可注入以保证离线测试）
- HTTP Collector：execution 链接入 enforcement（含 `execution.py` 2 行
  additive 透传，需例外批准）；仅页码分页、仅改 page 参数的既有语义不变
- Evidence Artifact：policy 快照与解析审计进入证据通道（additive，
  不动既有 12 个 artifact 名）
- Public URL safety policy：robots、每域限速、重定向逐跳重校验
- crawler runtime integration：observe / curl_import 接线 enforcement，
  loopback 分支保持 S2 快照行为逐字节不变

前置治理项：冻结基线重锚（人工确认 tag）+ `errors.SPECS` 注册新码 +
全量回归恢复绿。
