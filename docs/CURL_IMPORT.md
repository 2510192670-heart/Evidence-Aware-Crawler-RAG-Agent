# 本地 cURL 导入

## 实际闭环复核

2026-09-13 复测通过。`.venv/Scripts/python.exe frontend/qa_console.py --live --curl` 以真实浏览器完成 cURL 输入、解析预览、创建任务、下载 result.json 与 report.json、校验两文件 SHA256、核对 source=curl_import，并验证刷新后仍选中新任务。任务 a08ca901-0f6e-4b6e-b228-fc44a92f4375：3 页 30 条完整采集，1.19 秒，模型调用 1 次（输入 791、输出 90 tokens）；命令末尾的沙箱文件访问告警发生在脚本成功之后，不是失败。

新增可复现验收命令：`.venv/Scripts/python.exe frontend/qa_console.py --live --curl`。该命令会创建一次真实云任务；以 POST 返回的新任务 ID 进行跟踪，失败立即退出，成功后实际下载 result.json 与 report.json，逐条核对商品、验证两文件 SHA256、核对 source=curl_import，并验证刷新后仍选中新任务。未传 --live 的模拟测试不调用云模型。

根因排查：此前 689e6965-0daf-46b5-aff9-b8131eda9913、f69385c2-a54f-4e7f-af18-3ff01f07da27、1e260259-437a-473d-b38b-1df722ed68e3、afa3c703-0466-4569-8dbd-d61b2995dd2a 在模型请求阶段约 10 秒返回 connect_timeout。同机排查显示 DNS 解析正常（仅 IPv4）、裸 TCP+TLS 到两个 A 记录各 6 次全部 0.01～0.04 秒成功、httpx 连续 10 次 HTTPS 请求全部 0.10～0.32 秒成功、新进程独立连通检查 1.3 秒成功、运行中 API 连续多次真实任务成功。判定为时间上间歇的对外连接波动，命中 10 秒 connect 超时，既不是模型生成或 JSON 解析失败，也不是运行中 API 进程退化。

应对：网关仅对 connect_timeout / network_error 这类短暂连接故障重试一次；鉴权、限流、5xx 与读写/池超时行为不变，重试仍占用 4 次调用预算。超时拆分（connect/read/write/pool）保留。

控制台创建表单顶部展开“从 cURL 导入请求”。使用浏览器 Copy as cURL (bash) 格式。

可直接试用的本机样例：

```bash
curl 'http://127.0.0.1:8000/api/products?page=1&page_size=10' -H 'Accept: application/json'
```

POST JSON 分页同样可导入（页码成员位于请求体顶层）：

```bash
curl 'http://127.0.0.1:8100/catalog/v1/query' -X POST \
  -H 'Content-Type: application/json' \
  --data-raw '{"page": 1, "page_size": 10}'
```

点击“解析并预览”。成功后显示 URL 与参数，页面地址和翻页按钮禁用。再点击“创建任务”，直接读取接口样本，交给现有模型计划、RAG 和分页执行流程。提取字段与页数仍使用表单设置。修改 cURL 会使预览失效；清除导入恢复浏览器观察模式。

解析接口 POST /api/v1/import/curl 接收 {"text":"curl ..."}，只解析文本，不发起目标请求，不调用模型，不创建任务。原始 cURL 不持久化；创建任务时透传并通过验证的请求元数据（imported_url，以及 POST 的 imported_method/imported_request_body），不再次解析 cURL 文本。不要在 URL 的普通字段中放置私密数据，按敏感字段名过滤不能识别所有秘密。

当前支持：单个本机 HTTP URL、GET、POST(JSON)、-X/--request、-H/--header、--url、--compressed、-d/--data/--data-raw/--data-binary、Bash 反斜线续行。除 Accept: application/json 或 */* 与 Content-Type: application/json 之外的请求头会阻止导入，不能静默丢弃。POST 必须是顶层 JSON 对象请求体，并显式声明 Content-Type: application/json；表单、multipart、非 JSON 请求体、敏感键名、@文件、Cookie、Authorization、输出文件、重定向选项、多个 URL 和 shell 表达式均不支持。PowerShell/CMD 专有引用语法不支持。上限 16 KiB；任务 URL 上限 512 字符。

模型仍只收到响应结构摘要，不收到 cURL 原文。报告 source=curl_import 标识来源。此模块未调用 spidertools.cn，也没有执行 shell 命令。

POST 成功任务同样会导出独立 collector.py：POST 使用单独模板（仅标准库、回环、application/json、只修改页码成员），输出 schema 与 GET 采集器一致。

验证：250 项 Python 测试通过，包括网关连接故障重试、GET/POST cURL 解析、阻止不支持输入、敏感内容不回显、无密钥预览、预览不创建任务、模拟 HTTP 接口样本采集、POST 导入元数据透传与任务编排（GET 默认元数据回归、敏感请求体在 API 边界拒绝且不回显），以及 S2 POST JSON 内部执行与导出 collector 的逐项等值比对。TypeScript 与 Vite 构建通过。

限制：真实浏览器“导入—创建—下载”已于 2026-09-13 复测通过。测试站若未运行，按 CONSOLE.md 启动 8000 端口服务后可手动试用。
