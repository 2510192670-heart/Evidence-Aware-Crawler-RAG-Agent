# 贡献指南

请先读 [README](README.md)、[行为准则](CODE_OF_CONDUCT.md)及[安全政策](SECURITY.md)。

## 范围与权利

新功能，尤其放宽目标、认证、POST、修复或数据政策的变更，先提出范围、理由、替代方案和风险。不得删除测试、弱化断言或绕过验证。

只提交你有权贡献的代码、文档与合成数据，同意按本仓库 MIT 提供，保留第三方声明；不要求转让版权。不提交凭证、真实数据库、个人资料或未经授权的截图。

## 工程约束

模型只提出声明式计划；代码负责校验执行。复用脱敏、目标校验及错误契约，保持 BM25、一次修复边界和既有 JSON 兼容。基准、案例、collector 模板及安全变更需专项评审。

AGENTS.md 包含历史里程碑约束；当前工作台范围见 docs/superpowers/plans/2026-09-16-workbench-v1.md。范围冲突应先澄清，不自行扩展。

## 检查与 PR

按 INSTALL.md 安装后运行：

```powershell
.venv/Scripts/python.exe -m pytest tests -q
npm.cmd --prefix frontend run build
.venv/Scripts/python.exe frontend/qa_console.py
git diff --check
```

已知一项历史冻结失败必须如实报告，不能跳过后宣称全绿。真实公网/模型验收需单独明确执行和报告调用数。

PR 说明问题、最终行为、验证、兼容影响、依赖/权利及限制，保持聚焦。提交信息可用 feat、fix、docs、test 前缀。漏洞首次披露不使用公开 PR，先私下报告。
