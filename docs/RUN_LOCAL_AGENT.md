# 运行本机网页分析闭环

当前可以运行：Playwright 打开学习站并点击下一页 → 生成字段形状摘要 → DeepSeek Flash 生成计划 → 校验计划 → httpx 分页采集 → 保存数据和报告。

本文描述独立 CLI。任务 API 和数据库现已实现，见 TASK_API.md；Vue 控制台、RAG、自动修正和采集脚本导出仍未完成。

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

页面请求仅允许同 origin 的 GET，请求头含凭证或查询键含敏感字段时不作为候选。其他本机站点属于未验证的兼容性试验，并不保证成功；POST/登录/游标不在当前范围。

## 4. 查看产物

每次输出位置为 `data/tasks/{task_id}/`：

- `evidence.json`：请求 ID、参数变化、响应字段形状；不保存完整响应或请求头。
- `cloud_payload.json`：实际交给模型的业务输入，可核查上传范围；系统 schema/格式指令由网关附加。
- `plan.json`：模型提出的计划；是否验证成功以 report 为准。
- `result.json`：成功采集的数据；部分采集也会保存，完整性见报告。
- `report.json` / `report.md`：状态、条数、完整性、时间与用量；失败也生成报告。

云输入使用类型占位符，如 `<string>`、`<integer>`；不传商品名称值、本机 origin、Cookie 或认证头。保留字段名和短数字查询值是为了推断分页；这一策略针对自建测试站，不等于任意网站的通用隐私脱敏器。

完整性：`complete` 表示符合接口声明的 total；`partial` 表示达到用户页数上限但还有下一页；`stop_condition_only` 表示接口无 total，只能确认满足停止条件。

模型输出必须引用真实 request_id 和已观察的分页参数；字段路径采用 JSON Pointer，执行前在实际响应样本上验证。页面重定向不由执行器自动跟随。所有操作受 180 秒总时限控制，云端仅在连接类短暂故障（connect_timeout / network_error）时重试一次。

## 5. 已完成的真实验证

任务 `a093432d-4710-4e82-8e5e-3c0310820c53`：

- 真实浏览器捕获初始页与翻页请求。
- DeepSeek Flash 选择 req_001，识别 `/items`、page、`/has_next` 和 `/total`。
- 3 页共 30 条，完整性 complete。
- 独立检查 ID 1～30、商品名称和价格全部匹配固定靶场答案。
- 总耗时约 4.61 秒，模型调用 1 次；输入 660 tokens，输出 75 tokens。
- 另一次最小 API 连通性检查用量为输入 113、输出 5 tokens。

这些是本机单次运行结果，不是通用成功率、性能保证或费用报价。

58 项本地测试通过，`pip check` 通过。现有 FastAPI/Starlette 测试客户端仍有两项弃用提示，不影响本轮测试结果；未为消除提示额外升级框架。

## 6. 下一阶段

任务状态/API 和持久化已完成，接下来增加精选案例 RAG 与 Vue 界面。公网目标策略、POST 查询和脚本导出分别补测试后加入；现有学习站继续作为回归基线。
