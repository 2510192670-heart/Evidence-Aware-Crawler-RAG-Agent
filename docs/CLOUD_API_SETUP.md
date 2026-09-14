# DeepSeek Flash 接入说明

本项目已选择“本地工具 + DeepSeek Flash 云端 API”。浏览器、请求执行、RAG 和文件保存在本机，模型负责分析脱敏摘要。

## 当前状态

已实现配置、通用 Chat Completions JSON 网关、Flash 预设和独立连通性检查命令。离线测试覆盖成功、限流、鉴权、超时、截断、数据格式错误、取消和请求预算；已完成真实账号连通性验证；本机 GET 测试站的 CLI 闭环也已跑通，见 RUN_LOCAL_AGENT.md。仍不是完整初版产品。

未安装 Ollama、未下载本地生成模型、未增加 Python 依赖。已新增本机测试站观察、字段形状摘要和业务计划；通用网关本身不是自动脱敏器，业务层不得直接传入完整 HAR 或含凭证的数据。

## 官方参数（2026-09-12 核验）

| 配置 | 当前预设 |
|---|---|
| API 根地址 | `https://api.deepseek.com` |
| 请求地址 | `https://api.deepseek.com/chat/completions` |
| 模型 ID | `deepseek-flash` |
| response_format | `{"type":"json_object"}` |
| thinking | `{"type":"disabled"}` |
| stream | `false` |
| max_tokens | `1000` |

官方文档说明 Flash 是服务别名，底层版本可能更新。因此实验需要记录运行日期和模型 ID，不能把相同别名视为永远相同的模型权重。初版不使用旧别名。

关闭 thinking 是本项目的初始配置，不是官方默认值；官方默认开启。先用非 thinking 做基线，后续再比较开启后对准确率、耗时和用量的影响。JSON 模式仍可能空输出或截断，本地网关会拒绝这些结果，不直接执行。

## 1. 离线检查（无费用）

```powershell
Set-Location E:\PaChongLLMragzuoping
.\.venv\Scripts\python.exe -m backend.app.llm.check
```

默认不会请求云端，不需要密钥。

## 2. 真实连通性检查（一次 API 请求）

先在 DeepSeek 官方控制台创建 API Key，确认账号可用额度。不要把密钥发送到聊天或写进前端代码。

```powershell
Set-Location E:\PaChongLLMragzuoping
.\.venv\Scripts\python.exe -m backend.app.llm.check --live
```

优先使用环境变量 `WDA_LLM_API_KEY`，否则使用 `DEEPSEEK_API_KEY`；两者都没有时，命令会在终端隐藏输入密钥；输入后按回车，密钥仅保存在该次 Python 进程内存中，不写文件。

此命令只发送固定测试内容：要求返回 `{"ok":true}`，不读取网页、目录或项目源码。输出包含状态、模型名、输入/输出/缓存 token 和耗时；缺失的 usage 字段显示 null，而不是 0。

`--live` 会发送真实请求，可能产生费用；仅连接类短暂故障（connect_timeout / network_error）自动重试一次，其他错误不重试。超时和取消不能保证服务商未计费。不要把不断重跑当成错误修复方式。

## 3. 后端长期运行时的配置

后续启动 Agent 后端时，由启动进程环境提供 `DEEPSEEK_API_KEY` 或 `WDA_LLM_API_KEY`（项目变量优先）。系统变量新设置后，旧终端可能需要重启或显式载入，参见 RUN_LOCAL_AGENT.md。其他变量可选：

```powershell
$env:WDA_LLM_BASE_URL = 'https://api.deepseek.com'
$env:WDA_LLM_MODEL = 'deepseek-flash'
$env:WDA_LLM_RESPONSE_MODE = 'json_object'
$env:WDA_LLM_OUTPUT_TOKEN_FIELD = 'max_tokens'
$env:WDA_LLM_THINKING = 'disabled'
$env:WDA_LLM_TIMEOUT_SECONDS = '120'
$env:WDA_LLM_MAX_OUTPUT_TOKENS = '1000'
```

当前程序不自动读取 `.env`。`.gitignore` 已忽略 `.env`/`.env.*`，这只是防误提交，不表示已经实现配置文件加载。连通性检查的隐藏输入不会传回父终端，因此下一次运行可能需要重新输入，这是预期行为。

不要把 base URL 填成完整 `/chat/completions` 地址，网关会自行拼接该路径。也不要在 URL 中附带 key、账号密码或查询参数。使用代理是独立配置需求；当前 `trust_env=False`，不会自动采用系统代理环境变量。

## 4. 错误码

| code | 含义/处理 |
|---|---|
| authentication_failed | 401，检查本机配置的密钥 |
| access_denied | 403，检查账号权限与供应商策略 |
| rate_limited | 429，检查配额或稍后重试 |
| provider_error | 5xx，记录时间，查看官方服务状态 |
| request_rejected | 其他非成功 HTTP 响应，检查模型/参数与账号状态；此码不区分余额不足等具体原因 |
| redirect_rejected | API 重定向被拒绝，核对官方根地址 |
| connect_timeout / read_timeout / write_timeout / pool_timeout | 分阶段超时；connect_timeout 会重试一次，其余不重试；可能已计费 |
| timeout / network_error | 总时间失败或连接类网络错误（network_error 重试一次）；可能已计费 |
| invalid_output | 空内容、非法 JSON、schema 不符或响应结构不兼容 |
| output_incomplete | 截断、内容过滤或非正常完成；不执行输出 |
| input_too_large / response_too_large | 超过输入 16 KiB（含 schema/指令）或响应 256 KiB 上限 |
| call_budget_exceeded | 同一任务网关实例已发送 4 次请求 |

错误不回显供应商原始正文，避免凭证和数据进入日志。网关仅对 connect_timeout / network_error 这类短暂连接故障重试一次，且重试计入每任务 4 次调用预算；鉴权、限流、5xx 与读写/池超时不重试。

## 5. 费用控制与统计边界

- 每个任务网关实例最多发送 4 次请求；所有失败、超时与取消都占调用预算。
- 输出参数上限 1000 tokens，输入另有字节上限。字节与 token 不是同一个计数，不能把 16 KiB 宣称为某个精确 token 数。
- 分别记录 prompt_tokens、completion_tokens、prompt_cache_hit_tokens、prompt_cache_miss_tokens。缓存 token 属于输入 token 的分类，不应再与 prompt_tokens 相加重复计费。
- 当前不提供人民币金额估算或钱包硬限额：官方价格页本次读取失败，不能编造单价。真实费用以官方控制台/账单为准；上线前再核对价格、币种、计费日期和账号限额能力。
- 后续费用估算器须保存价格版本；usage 缺失或请求超时，应显示“费用未知”，而不是“免费”。
- 请求次数预算只是单任务防失控措施，不是账号每日总费用上限。多个任务会累积费用，后续任务数据库需要记录并实现每日调用限制。

## 6. 文件入口

- `backend/app/llm/config.py`：通用配置与 DeepSeek 预设。
- `backend/app/llm/gateway.py`：发送请求、调用预算、响应校验、用量统计。
- `backend/app/llm/check.py`：离线提示与一次真实连通性检查。
- `tests/test_cloud_gateway.py`：只使用模拟传输，不访问云服务。真实验证通过独立 CLI 执行。

后续路径：Playwright 请求证据 → 脱敏与筛选 → 本网关 + 业务计划 schema → 固定执行器 → 数据校验。供应商迁移只改预设或增加适配器；同名兼容 API 参数仍需重新测试。

## 官方资料

- [DeepSeek 首次 API 调用与当前模型 ID](https://api-docs.deepseek.com/)
- [JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [模型与价格（使用前在浏览器核对）](https://api-docs.deepseek.com/quick_start/pricing)
- [HTTPX 分段超时说明](https://www.python-httpx.org/advanced/timeouts/)
