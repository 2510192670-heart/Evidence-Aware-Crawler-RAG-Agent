# 安装与运行

主要验证环境：Windows / PowerShell、Python 3.11.2、Node.js 24.19.0。其他平台或版本需自行回归，不能据此认定兼容。

## 前提与安装

准备 Git、Python 3.11、Node.js 24/npm，以及访问 Python/npm/Playwright 下载源的网络。版本固定在 requirements.txt 和 frontend/package-lock.json。模型任务另需你有权使用的兼容 Chat Completions HTTPS API。

只在可信本机账号下运行。先读[安全政策](SECURITY.md)和[合规指南](docs/COMPLIANCE_CN.md)。不要在生产内网或含敏感凭证的环境中试跑不受信任页面。

```powershell
git clone https://github.com/2510192670-heart/Evidence-Aware-Crawler-RAG-Agent.git
cd Evidence-Aware-Crawler-RAG-Agent
git switch fix/curl-live-verification
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m playwright install chromium
npm.cmd --prefix frontend ci
./scripts/start_workbench.ps1 -PolicyFile ./config/public-policy.example.json
```

开发版在上述分支，main 可能仍是旧里程碑。脚本先构建 Vue，再以单 worker 启动 8002 端口。后端首次启动初始化/迁移 SQLite，不需要独立数据库服务器。打开[控制台](http://127.0.0.1:8002/console/)。

## 脚本受限时

若 PowerShell 策略阻止脚本，不必降低系统安全策略，可使用等价命令：

```powershell
npm.cmd --prefix frontend run build
$env:WDA_TARGET_POLICY_FILE = (Resolve-Path ./config/public-policy.example.json).Path
.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002
```

不启用多 worker、自动重载或公网监听。其他实例占用锁/端口时，先确认并正常停止自己的实例，不删除锁绕过保护。

## 模型与采集范围

在 UI 创建模型配置，填写名称、HTTPS API 根地址、模型 ID、密钥和供应商支持的输出选项，再选中配置。配置仅在内存，重启需重填；连接测试真实调用模型。

默认环境变量配置见 [CLOUD_API_SETUP.md](docs/CLOUD_API_SETUP.md)。应用读取进程环境，不自动加载 .env；不要把密钥写进 Git、截图或报告。无默认凭证时仍可启动控制台并用 UI 配置。

示例策略仅允许 books.toscrape.com。允许名单不是网站许可。新增目标前记录用途、字段、权利依据、期限及请求预算，再按[示例](config/public-policy.example.json)编辑并重启。不要扩大子域范围以规避检查。

首次试用填写 https://books.toscrape.com/，需求“提取图书名称、原始价格文本和详情链接”，字段 title、price、link；限制 1 页、3 条。检查样本后确认，核对来源与缺失，再下载。

预览计入 180 秒总时限，模型较慢时实际确认窗口可能不足 120 秒。超时看报告，不无限重试。真实任务和连接测试可能收费。

## 本地 JSON 靶场与验证

另开终端运行靶场，并在控制台选择本机范围：

```powershell
.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

开发检查：

```powershell
.venv/Scripts/python.exe -m pytest tests -q
npm.cmd --prefix frontend run build
.venv/Scripts/python.exe frontend/qa_console.py
```

当前有一项历史源码冻结断言失败，见[验收记录](docs/WORKBENCH_V1_ACCEPTANCE.md)，不能描述为全部通过。scripts/verify_workbench_live.py --live 会创建真实公网任务并调用默认模型，仅在明确需要付费验收时运行。普通 fixture 测试不需要密钥。

## 数据、升级与卸载

数据保存在 data/tasks/ 和 data/ 下的 SQLite，默认不自动过期。先停止服务器，再备份整个 data/ 并限制访问；升级前保留代码提交号和依赖锁文件。备份可能含完整结果、需求和来源，不能上传公开仓库。

升级需审查迁移、运行测试后启动；不在运行中覆盖数据库。卸载时停止进程、撤销专用 API 密钥，再由操作者选择删除项目、虚拟环境、下载和备份；不保证安全擦除。删除本地文件不删除供应商留存，应按其政策另行处理。
