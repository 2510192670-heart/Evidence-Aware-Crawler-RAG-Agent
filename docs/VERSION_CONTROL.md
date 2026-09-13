# Git 与 GitHub 版本管理

本地默认分支为 main。源码、测试、文档、requirements.txt 和前端 package-lock.json 纳入版本控制。虚拟环境、node_modules、dist、数据库、任务产物、日志与环境密钥文件不入库。

每次修改先检查差异，再提交一个完整功能或修复：

```powershell
git status
git diff
git add <明确的文件路径>
git diff --cached
git commit -m "描述具体变化"
```

重要变更建议创建分支：git switch -c feature/功能名。不要把真实 Cookie、Authorization 或 API Key 写进测试、文档或提交信息。Git 忽略规则不等于秘密扫描，也不能移除已提交历史中的秘密。

GitHub 远程仓库地址确定并完成身份验证后，添加 origin，再推送 main：git remote add origin <仓库HTTPS地址>，git push -u origin main。已有内容的远程仓库应先读取与比较，不能强制覆盖。

初始版本边界：本机 GET、页面观察、本地 cURL 导入、DeepSeek 计划、BM25 RAG、SQLite 任务服务、电脑端 Vue 控制台。导入真实闭环仍受到模型超时阻塞，详见 CURL_IMPORT.md。版本控制不代表所有功能已经验收或 GitHub 已发布。
