# 本地任务 API 使用说明

当前已实现：通过 API 创建任务、SQLite 历史记录、增量进度事件、取消、启动恢复与产物下载。任务复用之前已验证的 Playwright → DeepSeek → httpx 流程。Vue 控制台、RAG、采集脚本导出仍未实现。

## 启动

现阶段保持两个端口：商品靶场 `8000`，Agent API `8002`。主设计最终的端口重排将在前端集成时统一进行，本轮不打断已有学习站。

如果商品学习站已经在运行，不要重复启动：

```powershell
Set-Location E:\PaChongLLMragzuoping
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

另一个 PowerShell 运行 API：

```powershell
Set-Location E:\PaChongLLMragzuoping
# 仅当旧终端尚未继承你已经设置的系统变量时执行下面这行。
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'Machine')
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002
```

如果密钥在用户变量中，使用 `User`；项目变量 `WDA_LLM_API_KEY` 仍然优先。密钥不写入 SQLite。当前本机 API 已在后台启动，可以直接访问，无需另起实例。

访问 http://127.0.0.1:8002/docs 使用接口文档；文档界面的默认静态资源可能需要外网。API 本身的云调用需要网络，模型检查不会在健康检查中自动执行。

只启动一个 worker，不使用 `--workers 2`。同一数据目录有进程锁，第二个 API 实例会明确启动失败；锁文件留在磁盘是正常情况，操作系统锁会随进程退出释放。

## 创建一次任务

在 `/docs` 选择 POST `/api/v1/tasks`，使用默认请求即可。也可以用 PowerShell：

```powershell
$task = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8002/api/v1/tasks' -ContentType 'application/json' -Body '{}'
$task.id
```

POST 会执行真实分析，通常产生一次云模型调用；不要连续点击或重复提交当成查询。服务繁忙时返回 409，不自动排队。

默认请求：

```json
{
  "url": "http://127.0.0.1:8000/",
  "fields": ["id", "name", "price_fen"],
  "max_pages": 3,
  "click_text": "下一页"
}
```

任务创建返回 202 和 id。API 与 CLI 使用同一个模型配置和 GET 分页能力；仍只支持 HTTP 字面量 loopback 目标，不支持任意公网网站。

## 查询与取消

```powershell
Invoke-RestMethod "http://127.0.0.1:8002/api/v1/tasks/$($task.id)"
Invoke-RestMethod "http://127.0.0.1:8002/api/v1/tasks/$($task.id)/events?after_seq=0"
Invoke-RestMethod 'http://127.0.0.1:8002/api/v1/tasks?page=1&page_size=20'
```

事件 seq 单调递增，后续请求传上次收到的最大 seq。没有新事件返回空 items；正常完成阶段为：

`created → observing → analyzing → executing → verifying → succeeded`

取消：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8002/api/v1/tasks/$($task.id)/cancel"
```

取消会进入 cancelling，等待 worker 释放资源，最终返回 cancelled。重复取消是幂等操作；已完成任务不会变为 cancelled。取消只保证本地停止，云端请求可能已经计费。

API 正常退出时取消运行任务；如果进程被强制结束，下一次启动将遗留任务标记 interrupted，不自动重跑、不再次产生模型费用。历史终态保持不变，用户需要新建任务才能重跑。

## 下载产物

任务进入终态后：

```powershell
$artifacts = Invoke-RestMethod "http://127.0.0.1:8002/api/v1/tasks/$($task.id)/artifacts"
$result = $artifacts.items | Where-Object filename -eq 'result.json'
Invoke-WebRequest "http://127.0.0.1:8002/api/v1/artifacts/$($result.id)/download" -OutFile './downloaded-products.json'
```

列表返回产物 UUID、文件名、大小、SHA-256。服务器通过数据库映射读取固定文件名，校验路径和 hash，不接受任意磁盘路径。产物被外部修改后会返回 409 artifact_changed。文件只在任务完成清理并提交终态后公开，不将半写入文件作为完成结果发布。

产物白名单：7 个通用文件（evidence / cloud_payload / retrieval / plan / result / report.json / report.md），以及导出脚本产物 `collector.py`、`collector_result.json`、`collector_verification.json`。下载媒体类型按扩展名区分：`.json → application/json`、`.py → text/x-python`、其余 `text/markdown`。

`result.json` 的完整性要结合任务 summary.completeness 查看；partial 是用户页数上限内的结果。summary 另含 `collector_exported`、`collector_execution_success`、`collector_matches_internal_result` 三个布尔值：脚本导出、执行或比对失败只影响这些标记，不改变已成功任务的 status。取消/失败任务可能保留此前生成的证据或计划，这些产物不代表采集成功。

## 接口表

| 方法与路径 | 行为 |
|---|---|
| GET `/api/v1/health` | 检查数据库和本机配置，live_probe=not_performed；不调用模型 |
| POST `/api/v1/tasks` | 创建任务，202；忙时 409；模型未配置时 503 |
| GET `/api/v1/tasks` | 分页历史列表 |
| GET `/api/v1/tasks/{id}` | 状态、请求快照、结果摘要 |
| GET `/api/v1/tasks/{id}/events` | 按 after_seq 增量读取 |
| POST `/api/v1/tasks/{id}/cancel` | 幂等取消并等待清理 |
| GET `/api/v1/tasks/{id}/evidence` | 终态后读取脱敏证据，运行中返回空数组 |
| GET `/api/v1/tasks/{id}/artifacts` | 终态产物清单 |
| GET `/api/v1/artifacts/{id}/download` | hash 校验后下载 |

错误响应为 code/message/task_id/retryable，参数校验不回显敏感输入。当前未建立用户系统或跨机器访问权限，仅监听本机；拒绝不匹配的浏览器 Origin。

## 数据与迁移

- `data/agent.sqlite3`：tasks、task_events、artifacts 和 alembic_version。
- `data/tasks/{id}/`：每个任务的实际文件。
- `backend/migrations/versions/0001_tasks.py`：初始迁移；启动时自动执行 Alembic upgrade head。
- `.gitignore` 排除数据库、运行锁与任务产物。

数据库采用短写事务，模型/浏览器运行时不持有数据库事务。SQLAlchemy Core 封装访问，Alembic 记录结构版本；未来 PostgreSQL 迁移仍需数据核对与事务行为测试。

备份时先正常停止 API，再同时复制数据库与 tasks 目录。不要只复制文件或只复制数据库后声称备份完整。不要手动修改运行中的 SQLite 文件。

此前 CLI 创建的任务目录不自动导入数据库；API 历史列表从本轮开始记录。CLI 仍可独立运行。

## 本轮验证

- 72 项离线测试通过，涵盖原有模型网关/执行器与新增 API。
- 覆盖运行期互斥、重复取消、执行前取消、正常停机、重启 interrupted、同目录多实例拦截、历史读取、产物损坏拒绝下载。
- 真实 API 任务：`46c9141f-61a0-4375-8f63-c1a1b3d3ccd7`。
- 结果：3 页 30 条，ID/名称/金额逐条匹配靶场，6 个状态事件，6 个产物，下载 SHA-256 一致。
- 单次任务耗时约 5.16 秒，模型调用 1 次，输入 660 tokens、输出 90 tokens。缓存命中 512、未命中 148 属于输入分类，不应重复相加。
- 真实重启恢复通过离线测试验证；本轮没有为了复测重复发送云端任务。

### M3 脚本导出（2026-09-13）

- 114 项离线测试通过（原 100 + collector 导出 14）。
- 真实任务 `fc56bf6c-4a0b-473a-8743-aa7d7e060cf0`：3 页 30 条 complete；`collector_exported`、`collector_execution_success`、`collector_matches_internal_result` 均为 true；模型调用 2 次（首次连接类短暂失败后重试成功）。
- 通过 artifact API 下载 `collector.py`（媒体类型 text/x-python），脱离 Agent 独立运行得到 30 条 3 页 complete，与内部 `result.json` 逐条一致。
- 注意：浏览器观察路径依赖 Playwright，在受限环境（如工具沙箱）中可能超时；此时可用已支持的 cURL 导入路径完成任务。
- 已知限制（未来增强，非 M3 阻塞项）：`collector_verification.json` 目前只给出整体相等布尔与分项布尔，不包含逐条差异明细。

下一阶段：M4——POST JSON 页码分页与受限自动修正；随后是多结构靶场与冻结评测集的三组对照。脚本导出已完成，不能把固定 collect.py 当作本次自动导出的脚本。
