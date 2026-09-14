# Trace Viewer

Trace Viewer 是 task/events/artifacts 的只读投影页面，不产生新任务产物，不增加模型调用，不触发执行或修复。

## 访问方式

先按 [Console](CONSOLE.md) 构建前端并启动后端。在任务详情进入 Trace，或打开：

`http://127.0.0.1:8002/console/#/<task_id>/trace`

替换为 API 历史中存在的任务 ID。独立 CLI 的产物目录不会自动导入 API 历史。

读取接口：

- GET `/api/v1/tasks/{task_id}/trace`
- GET `/api/v1/tasks/{task_id}/artifacts/{filename}`

刷新快照与重试读取均为 GET，不重新创建、执行或修复任务。Trace 页面不是自动轮询的实时事件流。

## 页面结构与字段来源

| 区域 | 来源 |
| --- | --- |
| Overview | task 的 ID、状态、模型与时间；report 的条数、页数、调用数、耗时 |
| Timeline | task_events，经 trace projection 按 seq 投影 |
| Evidence Explorer | projection 从 evidence、plan、retrieval、result、report 提取摘要 |
| Retrieval Insight | analyzing 阶段投影；完整字段在 retrieval.json |
| Failure Diagnosis | failure_retrieval.json 的投影及原始 knowledge_gate |
| Repair Audit | report 投影与 repair.json 的实际审计字段 |
| Artifact Explorer | 已注册产物索引与 GET 读取的原始 JSON |

投影不是新的事实来源。摘要中的未知值显示“未记录”；原始 JSON 保留 null、空字符串、空数组、false 和 0，不把它们改写成推断结论。

## Timeline

仅含 Observing、Analyzing、Executing、Verifying 四个阶段。

- reached：记录中存在该阶段。
- failed：非成功终态下，投影标记的最后到达阶段；不等同于已精确定位内部失败调用。
- pending：非终态任务尚无该阶段记录。
- absent：终态任务缺少该阶段记录，不推断它执行成功。

`duration_s` 来自相邻记录事件时间差，不代表该模块独立运行耗时。无事件、无时间或产物尚未注册时保持缺失。

## Evidence Explorer

按阶段查看已有摘要，可跳到对应 JSON。Observing 展示来源与观察数量，Analyzing 展示计划字段、调用数和检索摘要，Executing 展示结果数量、页数和修复标记，Verifying 展示完整性与预期总数。

读取失败或缺失时不补造证据。任务成功与 collector 导出/比对成功需分别核对报告。

## Retrieval Insight

展示 enabled、algorithm、gate_applied 和 case_ids；展开 retrieval.json 查看完整特征及排序记录。检索命中不证明提高采集成功率。

默认冻结 v1 语料没有结构化 knowledge，knowledge gate inactive；不要将 fixture 的 active 状态描述为默认运行效果。

## Failure Diagnosis

读取错误码、类别、上下文阶段、失败阶段、案例 ID 及 knowledge gate 信息。诊断为 advisory only，不重新规划、不重新观察、不影响执行或修复，不新增 LLM 调用。

只有失败分支具备 summaries、RAG 开启且错误已分类等条件时才可能存在诊断产物。缺少产物不代表成功或无需诊断。

## Repair Audit

展示已记录的 attempts、proposals、报告和审计的 applied、outcome、candidate_plan_hash，以及策略 reason/rejection/validation。

LLM 提出初始计划，修复候选由确定性代码生成；repair.py 控制策略。仅符合全部条件的 pointer_not_found 可应用，最多一次；page_location_mismatch 仅提出候选，受限类别拒绝。页面不提供修复按钮。

## Artifact Explorer

显示已注册文件的大小与 SHA256 元数据，仅 JSON 可内联查看。collector.py、report.md 等文件仍由原控制台下载，不在查看器执行或渲染为富文本。

后端复用白名单与完整性检查：不存在返回 404，完整性校验失败或 JSON 不可读返回 409。投影可将不可读证据降级为缺失；展示索引 hash 本身不等于所有内容读取成功。

## 验证边界

使用说明根据当前源码整理。2026-09-14 已执行后端回归、前端构建、Trace fixture 测试、Console 模拟 QA 和已有真实任务读取；真实非空诊断/修复样本仍缺失。真实测试记录见 [M5.4 Evaluation Report](M5.4_EVALUATION_REPORT.md)，演示来源规则见 [Demo](DEMO.md)。
