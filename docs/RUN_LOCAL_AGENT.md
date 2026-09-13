# 运行本机网页分析闭环

当前可以运行：Playwright 打开学习站并点击下一页 → 生成字段形状摘要 → DeepSeek Flash 生成计划 → 校验计划 → httpx 分页采集 → 保存数据、报告和确定性 collector.py；也可用 cURL 导入重放 GET 或同源 POST JSON 请求。

本文描述独立 CLI。任务 API、数据库、Vue 控制台、BM25 案例 RAG 与确定性采集脚本导出均已实现，见 TASK_API.md；有限错误类型的确定性边界修复（M4.4 bounded deterministic repair）也已实现，模型只负责 proposal，执行由 deterministic validation 控制，详见 M4.4_EVALUATION_REPORT.md。检索自 M5.1 起为 BM25 + observation-aware feature gate：BM25 仍是排序主体，叠加确定性证据特征做结构匹配过滤与轻量重排（无 embedding / 向量数据库 / reranker / 额外模型调用）。M5.1 检索契约保持 legacy 字段并 additive 追加 `feature_schema_version` / `query_features` / `gate`。

## 准备

项目根目录是 `E:\PaChongLLMragzuoping`。使用 Python 3.11 虚拟环境。

```powershell
Set-Location E:\PaChongLLMragzuoping
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

当前电脑已经安装 Python Playwright，并复用了匹配的浏览器缓存；在新电脑执行上述安装即可。

## 1. 启动测试站

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

如果 http://127.0.0.1:8000/ 已能看到商品分页学习站，就不必重复启动。

## 2. 确认密钥进入当前终端环境

支持 `DEEPSEEK_API_KEY`，也支持项目变量 `WDA_LLM_API_KEY`（后者优先）。不用修改源码。

Windows 设置系统环境变量后，已经打开的 Codex/终端可能没有继承更新。可以重新打开终端；或者仅在当前 PowerShell 进程载入你已经设置的系统变量：

```powershell
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'Machine')
```

如果设置在“用户变量”中，把 `Machine` 改为 `User`。该语句不会打印密钥，也不修改持久环境配置。不要通过 echo 或 Get-ChildItem Env: 导出密钥。

## 3. 运行真实分析

```powershell
Set-Location E:\PaChongLLMragzuoping
.\.venv\Scripts\python.exe -m backend.app.pipeline.run
```

命令会发送一次真实模型请求，可能计费。每次运行生成新的任务 ID 和独立目录，不覆盖先前数据。

也可以只采集前两页：

```powershell
.\.venv\Scripts\python.exe -m backend.app.pipeline.run --max-pages 2
```

这时正常结果应是 20 条，完整性为 `partial`，而不是“已采集全部商品”。

可用参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| --url | http://127.0.0.1:8000/ | 当前仅接受 HTTP 字面量 loopback：127.0.0.1 或 ::1；不接受 localhost/公网域名 |
| --fields | id,name,price_fen | 希望输出的字段名，逗号分隔 |
| --click-text | 下一页 | 观察时点击一次的按钮名称；传空字符串可以跳过 |
| --max-pages | 3 | 最多 1～10 页 |

浏览器观察仅接受同 origin 的 GET 与同源 JSON POST；请求头含凭证、或查询键/请求体键含敏感字段时不作为候选。POST JSON body 页码分页已支持（计划声明 `pagination_location="json_body"`，也可通过 cURL 导入 POST）。登录、游标与 offset 分页仍不在当前范围；其他本机站点属于未验证的兼容性试验，并不保证成功。

## 4. 查看产物

每次输出位置为 `data/tasks/{task_id}/`：

- `evidence.json`：请求 ID、参数变化、响应字段形状；不保存完整响应或请求头。
- `cloud_payload.json`：实际交给模型的业务输入，可核查上传范围；系统 schema/格式指令由网关附加。
- `retrieval.json`：BM25 + observation-aware feature gate 的检索结果。legacy 字段：`enabled`、`algorithm`、`corpus_version`、`corpus_sha256`、命中案例 `cases`；M5.1 additive 追加 `feature_schema_version`、`query_features`、`gate`。关闭 RAG 时仅保留 legacy 字段且 `cases` 为空，输出与旧契约完全一致。
- `plan.json`：模型提出的计划；是否验证成功以 report 为准。
- `result.json`：成功采集的数据；部分采集也会保存，完整性见报告。
- `collector.py`：由已验证计划与固定模板生成的独立采集脚本；`collector_result.json` 是它的运行结果，`collector_verification.json` 记录与内部结果的比对。
- `report.json` / `report.md`：状态、条数、完整性、时间与用量；失败也生成报告。
- `repair.json`：有界修复审计（attempts / proposals / application 与计划谱系哈希）；仅当本次任务发生过修复尝试时才生成，是增量产物，不改动 `plan.json` 等既有产物契约。

云输入使用类型占位符，如 `<string>`、`<integer>`；不传商品名称值、本机 origin、Cookie 或认证头。保留字段名和短数字查询值是为了推断分页；这一策略针对自建测试站，不等于任意网站的通用隐私脱敏器。

完整性：`complete` 表示符合接口声明的 total；`partial` 表示达到用户页数上限但还有下一页；`stop_condition_only` 表示接口无 total，只能确认满足停止条件。

模型输出必须引用真实 request_id 和已观察的分页参数；字段路径采用 JSON Pointer，执行前在实际响应样本上验证。页面重定向不由执行器自动跟随。180 秒总时限仅约束核心采集流程（观察 → 证据 → 检索 → 计划 → 校验 → 执行 → 验证）；成功后的 collector 导出属于 best-effort 后处理，其子进程自身最多 30 秒，因此整个任务的 wall-clock 可能超过 180 秒。云端仅在连接类短暂故障（connect_timeout / network_error）时重试一次。

## 5. 已完成的真实验证

任务 `a093432d-4710-4e82-8e5e-3c0310820c53`：

- 真实浏览器捕获初始页与翻页请求。
- DeepSeek Flash 选择 req_001，识别 `/items`、page、`/has_next` 和 `/total`。
- 3 页共 30 条，完整性 complete。
- 独立检查 ID 1～30、商品名称和价格全部匹配固定靶场答案。
- 总耗时约 4.61 秒，模型调用 1 次；输入 660 tokens，输出 75 tokens。
- 另一次最小 API 连通性检查用量为输入 113、输出 5 tokens。

这些是本机单次运行结果，不是通用成功率、性能保证或费用报价。

本节为早期记录；当前 250 项本地测试通过。现有 FastAPI/Starlette 测试客户端仍有两项弃用提示，不影响测试结果；未为消除提示额外升级框架。

## 6. 多结构靶场与冻结评测清单（M4.1 已完成）

M4.1 建立了后续 M4/M6 共用的本机测试与评测基础，**未修改生产 pipeline**。新的结构全部放在独立目录 `fixtures/`，不再堆入根目录 `main.py`；既有学习站 `/api/products` 行为不变。

启动靶场（独立 FastAPI 应用，默认 `127.0.0.1:8100`）：

```powershell
.\.venv\Scripts\python.exe -m fixtures
```

- S1 `GET` 页码分页变体：路由名、分页参数名、每页参数名、字段名与 total/结束标记名都与学习站不同。
- S2 `POST` JSON 页码分页 fixture：端点与页面已就绪。M4.3.1 起浏览器观察会记录同源 JSON POST 证据；M4.3.2 起执行器支持页码位于 JSON body 顶层成员的分页（计划需声明 `pagination_location="json_body"`，GET 与 `json_body` 不得混用）。
- S3 嵌套响应：`/data/records` + `/data/totalCount` + `/data/hasMore` 一类结构，覆盖短末页与“只有 total、无结束标记”两种。
- S4 同 origin 干扰簇：一个真实分页接口 + 一个相似但不可分页的接口 + 一个 metadata/status 接口。
- S5 确定性漂移：重复唯一键、跨页 total 变化、字段类型变化、空页但结束标记为 true；全部由固定数据集产生，不使用随机数。

评测清单 `benchmarks/tasks.json` 共 20 个任务槽位（S1～S5 各 4 个，每类 2 个 `dev` + 2 个 `held_out`）：

- `solution` 是标准答案（方法、路由、分页参数、指针、唯一键），`expected` 声明预期条数/页数/完整性，或预期失败码。
- S2 任务在 M4.3.3 完成后已标记 `"supported": true`，并移除 `"unsupported_until"`；清单的能力声明与内容哈希一并更新。
- 内容哈希记录在 `benchmarks/tasks.sha256`；测试会重新计算 canonical SHA-256，任何任务改动都会被发现。
- 留出隔离由测试强制：dev / held_out 的路由名、字段名、分页键三者互不重叠，且留出任务的名称不会出现在 RAG 案例语料中。

M4.3.3 已完成 POST 的 cURL 导入、collector 导出与清单 capability 翻转；M4.3.4 进一步打通了导入请求元数据（method/request_body）到任务编排与执行。尚未完成：bounded repair（M4.4），以及规则 / 无 RAG / RAG 三组对照的最终运行与结论。M4.1 只提供冻结靶场与标准答案，对照结论属于 M6；错误分类基线见下一节。

## 7. 错误分类（M4.2 第一批）

`backend/app/pipeline/errors.py` 建立确定性错误分类注册表。错误码在抛出处决定，`category / repairable / retryable / detail_keys` 全部由注册表按码查出，调用方无法传入策略字段；未注册的码在构造时立刻失败，任何没有升级为 `PipelineError` 的异常一律归为 `UNCLASSIFIED`，默认不可修复、不可重试（fail-closed）。

- `PipelineError` 继承 `ValueError`，且 `str(error)` 严格等于错误码，因此既有异常测试与消息比较不受影响。
- 类别：`SECURITY`、`OBSERVATION`、`PLAN_SEMANTIC`、`DATA_INTEGRITY`、`TRANSPORT`、`MODEL_OUTPUT`、`BUDGET`、`UNCLASSIFIED`。
- 第一批只迁移 5 个码：`pointer_not_found`、`duplicate_id`、`total_changed`、`field_type_changed`、`empty_page_with_next`。其余 raise 点保持裸 `ValueError`，被安全地归为 `UNCLASSIFIED`，不会误开修复入口。
- M4.3.2 新增两个 PLAN_SEMANTIC 码 `page_location_mismatch` 与 `invalid_page_field_type`（新码，不是迁移）。`unobserved_page_parameter` 仍是裸 `ValueError`，分类与迁移批次保持不变。
- `details` 只是结构上下文（页号、字段名、唯一键、指针），有 key 白名单、仅接受标量、超长截断，绝不包含响应值或凭证。
- 失败报告只新增字段，不改变既有字段：`error`（稳定错误码）、`error_type`、`error_category`、`error_repairable`、`error_retryable`。未迁移路径的 `error` 仍是异常类型名。
- 导出的 `collector.py` 仍完全不依赖本项目模块，`export.py` 也不依赖 `errors.py`。

## 8. 下一阶段

任务状态/API、持久化、案例 RAG、Vue 控制台、cURL 导入（GET/POST）、确定性 collector 导出（GET/POST）、M4.1 多结构靶场与冻结评测清单、M4.2 错误分类基线、M4.3.1 POST 证据层、M4.3.2 POST 计划与执行、M4.3.3 POST cURL 导入与 collector 导出与清单翻转、M4.3.4 导入元数据贯通任务编排、M4.3.5 安全加固与 M4.3 freeze，以及 M4.4 有界确定性修复均已完成。M4.4 只把 `pointer_not_found` 加入自动应用白名单，`page_location_mismatch` 仅生成 proposal，SECURITY / DATA_INTEGRITY / TRANSPORT / UNCLASSIFIED 一律拒绝；模型不参与执行决策，也没有第二次 LLM 调用或无限重试。M4.4 Final Freeze 已收口，M5 尚未开始。公网目标策略另行设计，现有学习站继续作为回归基线。
