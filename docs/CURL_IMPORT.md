# 本地 cURL 导入

## 实际闭环复核

2026-09-13：已通过真实 Playwright 浏览器完成 cURL 输入、解析预览、创建任务，服务端成功读取本机接口样本，报告 source=curl_import。两次任务 689e6965-0daf-46b5-aff9-b8131eda9913、f69385c2-a54f-4e7f-af18-3ff01f07da27 均在模型请求阶段约 10 秒超时，未生成成功采集结果，故结果下载闭环仍未通过。用量返回 null，不能视作免费或零消耗。

独立 Python HTTPX 无凭证连接模型根地址返回 401，说明该进程当时可连接，但不足以证明运行中 API 的模型请求正常。尝试重启 API 并明确模型地址和超时配置的命令遭自动审批拒绝（blocked by policy，无具体原因），未执行；超时根因未确认。

控制台创建表单顶部展开“从 cURL 导入请求”。使用浏览器 Copy as cURL (bash) 格式。

可直接试用的本机样例：

```bash
curl 'http://127.0.0.1:8000/api/products?page=1&page_size=10' -H 'Accept: application/json'
```

点击“解析并预览”。成功后显示 URL 与参数，页面地址和翻页按钮禁用。再点击“创建任务”，直接读取接口样本，交给现有模型计划、RAG 和分页执行流程。提取字段与页数仍使用表单设置。修改 cURL 会使预览失效；清除导入恢复浏览器观察模式。

解析接口 POST /api/v1/import/curl 接收 {"text":"curl ..."}，只解析文本，不发起目标请求，不调用模型，不创建任务。原始 cURL 不持久化；创建任务时仅持久化通过验证的 imported_url。不要在 URL 的普通字段中放置私密数据，按敏感字段名过滤不能识别所有秘密。

当前支持：单个本机 HTTP URL、GET、-X/--request、-H/--header、--url、--compressed、Bash 反斜线续行。只允许 Accept: application/json 或 */*，执行器统一请求 JSON。其他头会阻止导入，不能静默丢弃。POST、数据体、Cookie、Authorization、文件读取、输出文件、重定向选项、多个 URL 和 shell 表达式均不支持。PowerShell/CMD 专有引用语法不支持。上限 16 KiB；任务 URL 上限 512 字符。

模型仍只收到响应结构摘要，不收到 cURL 原文。报告 source=curl_import 标识来源。此模块未调用 spidertools.cn，也没有执行 shell 命令。

验证：94 项 Python 测试通过，包括解析、阻止不支持输入、敏感内容不回显、无密钥预览、预览不创建任务、模拟 HTTP 接口样本采集。TypeScript 与 Vite 构建通过。

限制：本轮真实浏览器“导入—创建—下载”验证命令被自动审批系统拒绝（blocked by policy，无具体原因），未完成此项验证。不能把以前的普通页面采集测试当作本次导入测试。测试站若未运行，按 CONSOLE.md 启动 8000 端口服务后可手动试用。
