# Observe Analyze Execute Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline. Steps use checkbox syntax. This is a single local CLI slice, not the entire planned UI/server release.

**Goal:** 在现有本机商品靶场上，真实捕获网络请求，通过 DeepSeek Flash 选择接口和字段，再用本地程序采集并验证数据。

**Architecture:** 保留当前 FastAPI 靶场。新增独立 CLI，组合 Playwright 观察、脱敏摘要、业务计划 schema、现有云网关、固定请求执行器和任务文件目录。初版只接受显式本机 loopback HTTP 页面，不宣称具有公网目标策略或数据库任务调度。

**Tech Stack:** Python 3.11、Playwright、httpx、Pydantic、DeepSeek Flash。

**Spec:** [项目设计](../../PROJECT_DESIGN_V0.1.md)，实现 M1/M2 的 GET 页码分页纵向切片。

## Global Constraints

- 保留现有学习站与 14 项基线测试。
- `DEEPSEEK_API_KEY` 受支持，`WDA_LLM_API_KEY` 显式配置优先；不打印密钥。
- 新任务一次分析调用；不自动重试云模型；总时限 180 秒，最多 10 页。
- 浏览器请求仅允许目标 origin；执行器不跟随重定向；只支持 GET 查询页码分页。
- 向模型传字段形状、参数变化及脱敏样本；不上传请求头、完整响应和本地路径。
- 失败也保存短错误报告；不得把部分结果标为全量。

## Task 1: 标准环境变量

- [x] `tests/test_cloud_gateway.py` 先测试 DeepSeek 标准密钥和项目密钥优先级。
- [x] `config.py` 增加 fallback，`check.py` 接受两种变量；复测。
- [x] 系统级变量只在执行子进程中读取，完成一次真实连通性检查。

## Task 2: 计划、脱敏与执行

Files: `backend/app/pipeline/contracts.py`, `execution.py`, `tests/test_pipeline.py`。

Interfaces: `ExtractionPlan` 包含 request_id、items_pointer、fields、unique_key、page_parameter、has_next_pointer、total_pointer；`execute_plan(client, plan, observations, max_pages)` 返回 items/completeness/pages。

- [x] 写未知 request_id、非法 JSON Pointer、敏感字段、重复 ID、完整采集和上限截断测试，运行确认失败。
- [x] 实现 JSON Pointer 解析；执行目标从捕获记录读取；分页参数必须出现在实际查询中。参考计算：`query = dict(record.query); query[plan.page_parameter] = str(page)`。
- [x] 每页流式读取上限 256 KiB，最多 10 页，唯一键查重；total 变化/缺页/非 JSON 拒绝，已达上限标 partial。
- [x] 用 MockTransport 对照既定数据验证，不调用云服务。

## Task 3: 浏览器观察

Files: `backend/app/pipeline/observe.py`。

Interface: `async observe(url: str, click_text: str | None) -> list[Observation]`。

- [x] 在导航前注册 response 监听，route 限制同 origin，创建干净 context 并禁用 service worker；记录前后两次 GET JSON 请求。
- [x] 观察窗口与 response 处理受总超时控制；任务数、响应大小有上限；finally 关闭浏览器。
- [x] 脱敏摘要只用字段形状，字符串值替换占位符；捕获记录留在本机。包含敏感查询字段的请求不用于执行。
- [x] 实际学习站验证 request_id 与两页查询差异，保持先监听再点击。

## Task 4: CLI 闭环与产物

Files: `backend/app/pipeline/run.py`, `backend/app/pipeline/__init__.py`。

- [x] CLI 接收 URL、目标字段、点击按钮文字、最大页数；默认现有商品靶场。
- [x] 通过 `CloudGateway.generate` 生成计划；仅把脱敏摘要写入 cloud_payload.json；校验后执行。
- [x] 保存 evidence.json、plan.json、result.json、report.json、report.md 到 `data/tasks/{UUID}`。报告含用量、耗时与完整性。
- [x] 真实 DeepSeek 执行一次，独立校验 30 个 ID、商品名与价格；失败修复后才重新运行，避免无理由付费重试。

## Task 5: 文档与回归

- [x] 固定新增 Playwright 依赖和浏览器安装说明。
- [x] README 写明 CLI 用法、环境变量继承问题、当前只支持本机 GET 的边界；主设计同步实现状态。
- [x] 运行全部测试，核查产物不含密钥，报告真实与模拟验证的差别。

本切片不实现 Vue、SQLite、RAG、导出脚本、POST、游标分页或公网目标支持；这些仍按主设计后续里程碑推进。

## 执行结果

58 项测试通过；真实 CLI 任务 a093432d-4710-4e82-8e5e-3c0310820c53 完成，30 条数据独立校验一致。采集使用匹配的 Playwright Chromium 缓存。浏览器 body API 整体读取，256 KiB 为保留大小上限而非浏览器网络内存硬限制，此边界已在代码注明。
