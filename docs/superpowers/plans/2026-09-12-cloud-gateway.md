# Cloud Model Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. Execute inline for this small module; no parallel agents needed.

**Goal:** 实现可以离线验证、供应商可配置的云端 JSON 模型网关，不进行真实付费调用。

**Architecture:** 保留现有靶场，在 backend/app/llm 中新增适配器。后端读取环境变量，通过 httpx 发送 Chat Completions 兼容请求，用 Pydantic 校验输出；预算按任务级网关实例隔离。模型网关只接收调用者已筛选的结构化内容，后续观察模块负责脱敏。

**Tech Stack:** Python 3.11、httpx、Pydantic 2、pytest；全部复用当前依赖。

**Spec:** [初版设计](../../PROJECT_DESIGN_V0.1.md)，第 5、7、10 节。

## Global Constraints

- 云端 API 是主路线；不安装 Ollama、不下载生成模型。
- 网关本阶段仅提供传输/校验能力；不宣称已完成 Agent 或真实供应商兼容验证。
- 每任务最多 4 次模型调用，单次总超时 120 秒，输出最多 1000 tokens。
- 使用 HTTPS、禁跟随重定向、不记录 API Key/原始响应，不自动重试。
- 没有真实供应商配置时使用 MockTransport，不搜索或复用其他项目密钥。
- 不改现有靶场端口与采集脚本，原有 14 项测试继续通过。
- 本目录当前无 Git 仓库，不为本次工作初始化仓库或制造提交。

## Task 1: 配置与错误契约

**Files:** Create `backend/__init__.py`, `backend/app/__init__.py`, `backend/app/llm/__init__.py`, `backend/app/llm/config.py`; Test `tests/test_cloud_gateway.py`。

**Interfaces:** `CloudConfig.from_env(env: Mapping[str, str] | None = None) -> CloudConfig`；配置字段 base_url、api_key（repr 隐藏）、model、response_mode、output_token_field、timeout_seconds、max_output_tokens。变量 WDA_LLM_BASE_URL、WDA_LLM_API_KEY、WDA_LLM_MODEL 必填；其他取文档默认值。

- [x] 写缺少配置、HTTP URL、带 userinfo/query 的 URL、空模型/密钥和配置 repr 不泄漏密钥测试。

```python
def test_missing_config():
    with pytest.raises(ValueError):
        CloudConfig.from_env({})
```

- [x] 运行 `.\.venv\Scripts\python.exe -m pytest tests/test_cloud_gateway.py -q`，确认模块缺失导致失败。
- [x] 用 dataclass 实现配置，使用 urlsplit 验证 https 和 hostname，拒绝 userinfo/query/fragment；将 base URL 末尾斜杠移除，错误只包含变量名称，不回显值。

```python
@dataclass(frozen=True)
class CloudConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    response_mode: str = 'json_object'
    output_token_field: str = 'max_tokens'
    timeout_seconds: float = 120
    max_output_tokens: int = 1000
```

- [x] 重跑该文件测试；确认配置实例不修改进程环境、不加载未知密钥文件。

## Task 2: 请求、预算与校验

**Files:** Create `backend/app/llm/gateway.py`; Extend `tests/test_cloud_gateway.py`。

**Interfaces:** `CloudGateway(config: CloudConfig, transport: httpx.AsyncBaseTransport | None = None)`；`async generate(payload: dict, output_schema: type[BaseModel]) -> ModelResult`；`ModelResult.data` 为校验后的模型，`usage` 为输入/输出 token 或 None，`model` 为配置 ID；`GatewayError.code` 为稳定错误码。一个 gateway 实例归属一个任务，不全局复用预算。

- [x] 用 MockTransport 编写请求体、URL、Bearer、JSON 输出校验、usage 缺失测试。

```python
def handler(request):
    assert request.url.path == '/v1/chat/completions'
    return httpx.Response(200, json={
        'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 5},
    })
```

- [x] 增加 401/403/429/500、截断、空返回、非法 JSON、schema 不符、网络超时、取消与调用上限测试；先运行确认失败。
- [x] 实现 `asyncio.timeout` 总时间限制，`httpx.AsyncClient` 分段超时，`follow_redirects=False`、`trust_env=False`；只发送一次，不自动重试。输入 JSON 字节超过 16 KiB 或响应超过 256 KiB 时拒绝；用逐块读取限制响应。
- [x] 发送前增加 calls，使用 async lock 防止并发越界；usage 有效时记录，否则记 None，失败请求也占调用次数。输出模式只允许 json_object、json_schema、none；token 字段只允许 max_tokens 或 max_completion_tokens。

```python
body = {
    'model': config.model,
    'stream': False,
    config.output_token_field: config.max_output_tokens,
    'messages': messages,
}
# json_schema 仅在所选供应商确认支持时使用；none 仍要求 JSON 并在本地校验。
```

- [x] 将响应结构和输出验证异常映射到不包含正文的 GatewayError，保留取消异常传播；运行网关测试与原有测试。

## Task 3: 使用说明与设计同步

**Files:** Create `docs/CLOUD_API_SETUP.md`; Modify `.gitignore`, `README.md`, `docs/PROJECT_DESIGN_V0.1.md`。

**Interfaces:** 无新增 HTTP 路由。后续 ModelGateway.analyze 将组合此基础网关和业务 ExtractionPlan；本阶段不假定已存在分析器。

- [x] 文档给出供应商、base URL、model、后端环境变量、模式和 token 参数配置说明；密钥通过终端隐藏输入设置，不能粘贴进对话、前端或源文件。

```powershell
$env:WDA_LLM_MODEL = '供应商实际模型 ID'
$env:WDA_LLM_RESPONSE_MODE = 'json_object'
```

- [x] `.gitignore` 加入 `.env`、`.env.*`、保留 `.env.example`，以及 `data/tasks/`；不写真实配置。
- [x] 修订主设计的本地模型默认项；新增每任务请求上限、token/费用 unknown、账单对账和供应商限额说明。
- [x] 运行完整 pytest，扫描文档中的旧默认路线与占位标记；只报告离线测试成果，不声称真实 API 已通。

完成标准：现有学习站不回归；网关离线测试覆盖失败边界；供应商未配置不产生请求。真实联调依赖用户给出供应商/模型并在本机配置密钥，另行记录实际调用结果与费用。

后续实施顺序：M1 浏览器证据 → 组合本模块完成 M2 业务计划 → M3 任务/数据库 → M4 RAG → M5 Vue → M6 留出评测。本计划只交付云网关这一独立模块，未覆盖后续子系统的实现。

## 本轮执行结果

已完成通用网关、DeepSeek Flash 预设、独立检查命令和文档。用户在执行中选定 DeepSeek Flash，因此增加 thinking 显式禁用与缓存用量字段测试。43 项本地测试通过；只运行离线检查，未读取密钥、未调用真实云 API。供应商相关真实性能与费用留待本机配置密钥后的联调。
