# Web Data Agent

本地采集工具 + DeepSeek Flash API。已实现浏览器观察、采集计划与校验、BM25 案例检索、SQLite 任务管理、电脑端 Vue 控制台，以及本地 cURL 导入预览。

入口：http://127.0.0.1:8002/console/ 。使用方式见 [控制台说明](docs/CONSOLE.md)、[cURL 导入](docs/CURL_IMPORT.md)、[版本控制](docs/VERSION_CONTROL.md)。当前支持本机 GET 页码分页；最新 cURL 实际闭环在云端调用阶段超时，尚未验收通过。

下面保留早期学习阶段的记录，涉及“尚未实现”或测试数量的表述仅代表当时状态。

## 第 1 阶段：页面、分页接口与采集脚本

完整项目的初版范围、确定技术栈、模块接口、验收标准与后续迁移路线见 [初版项目设计](docs/PROJECT_DESIGN_V0.1.md)。下文保留学习原型说明；新增任务 API 见 [任务管理与下载](docs/TASK_API.md)。RAG 和 Vue 控制台尚未实现。

**已选路线：本地工具 + DeepSeek Flash API。** 已完成真实云调用、浏览器观察/计划/执行闭环，以及 SQLite 任务 API（创建、进度、取消、历史与下载）。3 页 30 条结果已通过 API 独立核验，72 项测试通过。[任务 API：8002/docs](http://127.0.0.1:8002/docs) · [CLI 运行说明](docs/RUN_LOCAL_AGENT.md) · [DeepSeek 配置](docs/CLOUD_API_SETUP.md)。支持 `DEEPSEEK_API_KEY`，无需本地生成模型。

目标：理解 URL 查询参数如何变成 Python 函数参数，以及服务端如何返回一页 JSON 数据。

## 环境

本项目使用 Python 3.11，依赖安装在 `.venv` 中，不需要激活环境。
首次在其他电脑使用时：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动

在 PowerShell 执行：

```powershell
Set-Location E:\PaChongLLMragzuoping
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

终端保持运行。按 Ctrl+C 停止。若端口被占用，改成 8001，并同步修改访问地址。

依次在浏览器打开：

- http://127.0.0.1:8000/api/products?page=1&page_size=10
- http://127.0.0.1:8000/api/products?page=2&page_size=10
- http://127.0.0.1:8000/api/products?page=4&page_size=10
- http://127.0.0.1:8000/api/products?page=0

预期分别是商品 1～10、11～20、空列表、422 参数校验错误。
`/docs` 是交互式接口文档，默认样式资源需要网络加载；上述 JSON 接口不依赖外部网络。

## 阅读 main.py

1. `FastAPI()` 创建应用，Uvicorn 负责监听端口并接收 HTTP 请求。
2. `@app.get('/api/products')` 把 GET 请求交给下面的函数处理。
3. `?page=2&page_size=10` 是查询参数，框架会转换成整数并验证范围。
4. `start = (page - 1) * page_size` 算出从第几个元素开始取。
5. `PRODUCTS[start:end]` 使用 Python 列表切片，包含 start，不包含 end。
6. 返回的字典会被框架编码成 JSON；`price_fen` 单位是分，避免浮点金额误差。
7. `total` 是全部商品数，`has_next` 表示是否还有下一页。

数据是固定的 30 条内存样本，不涉及数据库。

## 页面与 Network 观察

打开 http://127.0.0.1:8000/ 。页面默认展示商品 1～10，点击“下一页”依次展示 11～20、21～30。
第一页不能再向前翻，最后一页不能再向后翻。请求失败时保留原页，并提供重试按钮。

按 F12 打开开发者工具，进入 Network，选择 Fetch/XHR，再点击“下一页”。
查看请求 URL 中的 `page=2`，以及 Response 中的 `items`。
这就是“网页上的数据来自接口”的实际证据。页面源码在 `static/index.html`，没有前端构建依赖或外部 CDN。

## 运行采集脚本

保持服务终端运行，另开一个 PowerShell：

```powershell
Set-Location E:\PaChongLLMragzuoping
.\.venv\Scripts\python.exe collect.py
```

脚本从第一页开始，读取 `has_next` 决定是否继续。请求超时为 10 秒，最多读取 100 页。
脚本校验 HTTP 状态、字段类型、页码、总数稳定性、ID 唯一性和最终条数，全部通过后保存到 `data/products.json`。
输出文件会被下一次成功采集覆盖；校验失败时不会保存本次数据。
它只适用于这个学习站的固定接口结构，不是通用网站爬虫，也未接入 LLM。

也可以测试最后一页不足一页的情况：

```powershell
.\.venv\Scripts\python.exe collect.py --page-size 7
```

如果服务使用 8001 端口，增加 `--base-url http://127.0.0.1:8001`。
按 `main.py → static/index.html → collect.py → tests/` 的顺序阅读。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖三页完整性、ID 不重复、最后一页不足一页、超出范围的空页，以及非法查询参数。

## 动手练习

先预测再打开：`http://127.0.0.1:8000/api/products?page=3&page_size=7`

写出 start、end、返回的商品 ID 范围，以及 has_next 的值。
答案：start=14，end=21，ID=15～21，has_next=true。

下一阶段：使用 Playwright 自动记录网络请求，生成接口证据文件，再考虑接入模型。

参考：
- https://fastapi.tiangolo.com/tutorial/query-params-str-validations/
- https://fastapi.tiangolo.com/tutorial/testing/
# 阶段更新：本地案例 RAG

已接入可关闭的 BM25 案例检索，默认开启。使用方式与验证边界见 [本地 RAG 说明](docs/LOCAL_RAG.md)。API 创建任务传 `rag_enabled: false` 可关闭，任务产物新增 `retrieval.json`。Vue 控制台尚未实现。
# 最新入口：电脑端控制台

打开 http://127.0.0.1:8002/console/ ，直接创建任务、查看进度与历史、取消任务和下载结果。Vue 3 + TypeScript 控制台已接入真实 API；当前 80 项 Python 测试通过。见 [控制台操作与启动说明](docs/CONSOLE.md)。下方较早阶段的记录保留作开发过程参考。
