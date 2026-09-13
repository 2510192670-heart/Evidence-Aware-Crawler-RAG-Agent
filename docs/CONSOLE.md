# 电脑端任务控制台

入口：http://127.0.0.1:8002/console/ 。API 文档仍在 /docs；测试站仍使用 8000 端口。

技术栈：Vue 3、TypeScript、Vue Router、Vite、原生 CSS。依赖版本锁定于 frontend/package-lock.json。构建后由 FastAPI 同源提供静态文件，日常使用无需运行 Node 服务，也没有浏览器端 API Key。按用户要求面向电脑使用，最小页面宽度 960px；建议窗口宽度至少 1280px。

## 操作

1. 保持测试站与后端运行，打开控制台。
2. 默认页面地址、字段、3 页和“下一页”按钮可直接用于测试站。RAG 可勾选或关闭。
3. 点击“创建任务”，右侧自动显示进度。一次只能运行一个任务。
4. 完成后下载 result.json、报告、证据和检索记录。页面中显示完整性状态；失败或中断不会显示成成功。
5. 点击历史任务可切换详情，每页 5 个。刷新后地址中的任务 ID 保留选择。

界面每轮请求完成后间隔 1.5 秒刷新，避免重复轮询并发。单次请求设 20 秒超时。取消会等待服务端清理；云端已发出的请求可能仍计费。断网后显示提示，可重试。历史列表不是消息队列，后端仍负责最终并发限制。

## 启动与构建

在项目根目录分别打开两个 PowerShell 窗口：

```powershell
# 窗口一：测试站
.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

```powershell
# 窗口二：任务 API 与已构建的控制台
# 新终端通常会继承系统环境变量；若没有，显式读取，不要输出密钥。
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','Machine')
.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002
```

修改前端或首次从代码复制项目后，先构建再启动后端：

```powershell
cd frontend
npm.cmd ci
npm.cmd run build
```

frontend/dist 是生成文件，不纳入源码。若后端启动时尚未构建，根路径回退到 API 文档；首次构建后重启后端。开发热更新可运行 npm.cmd run dev 并访问其 /console/，API 通过 Vite 代理到 8002。不要开启公网监听。

## 验证

从根目录执行：

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe frontend/qa_console.py
```

第二项要求本地控制台已运行，拦截 API 使用模拟数据，验证空状态、创建、RAG 参数、运行中按钮禁用、取消、断网与重试，不消耗模型额度。只有显式添加 --live 才会通过实际页面创建一次云端任务并下载结果。

2026-09-13：100 项 Python 测试通过，TypeScript 检查与 Vite 构建通过。真实浏览器任务 331e5451-0be5-46c3-b953-3bab62d84ecd 完成并下载 30 条数据，刷新后选择保留；cURL 导入闭环复测见 CURL_IMPORT.md。浏览器无未捕获脚本异常。

内置浏览器工具返回 ERR_BLOCKED_BY_CLIENT，因此使用 Playwright Chromium 验证。检查了 1505×1045 概念原始尺寸和 1280×800 笔记本尺寸。早期手机检查不作为交付要求；用户已明确只在电脑使用。

## 视觉核对记录

概念：console-concept.png；最终页面：console-preview.png。使用 view_image 逐一核对布局、字体层级、配色、表格、间距和控件。

| 比较项 | 处理 |
| --- | --- |
| 左表单、右历史与详情 | 保留双栏及有边框的分区，表单占约 30% |
| 白底、深色文字、青色动作 | 使用一致色彩变量及状态颜色 |
| 标题与表单层级 | 主标题 38px，分区 21px，输入 15px，表格 14px |
| 进度与汇总 | 仅点亮实际事件，未知数值显示破折号 |
| 文案与数据 | 真实 UUID、时间和全部产物替换概念示例；删除“提升效果”未经验证的表述 |
| 功能必要差异 | 增加取消、错误、完整性状态、分页；展示所有 7 个真实文件使页面更长；下载区简化为文件名与下载动作 |

已按概念的布局和视觉方向核对实现；上述差异为真实功能和数据所需，没有把生成图片作为界面。没有宣称像素级一致。

下一阶段：增加多种本地测试接口与固定评测集，比较规则、模型、模型加 RAG 的正确率、完整率、token 与耗时。计划提示词仍面向本机 GET 页码分页；执行器自 M4.3.2 起也支持页码位于同源 POST JSON body 的分页，但 cURL 导入与 collector 导出尚未跟进，产品仍不是通用网站爬取工具。

实现依据：[Vite 静态构建](https://vite.dev/guide/static-deploy.html)、[Vue Router hash 模式](https://router.vuejs.org/guide/essentials/history-mode.html)。
