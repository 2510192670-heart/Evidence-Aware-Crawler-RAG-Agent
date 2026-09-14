# 本地案例 RAG 使用说明

当前链路：Playwright 观察 → 脱敏结构摘要 → 本地 BM25 检索 → DeepSeek 生成计划 → 本地验证与采集。

检索使用 rank-bm25 0.2.2 的 BM25Okapi。无向量模型、向量数据库或 GPU 需求；检索本身不调用云端模型。依赖与用法参考 https://github.com/dorianbrown/rank_bm25 。

## 使用

API 仍在 http://127.0.0.1:8002/docs ，测试站在 http://127.0.0.1:8000/ 。创建任务默认开启 RAG。POST /api/v1/tasks 请求体可填写：

```json
{"rag_enabled": true}
```

对照任务改成 false。CLI 默认开启，关闭用：

```powershell
.venv/Scripts/python.exe -m backend.app.pipeline.run --no-rag
```

任务新增 retrieval.json，可通过任务产物列表下载。legacy 字段：`enabled`、`algorithm`、`corpus_version`、`corpus_sha256`、`cases`（命中案例及分数）。M5.1 起 additive 追加：`feature_schema_version`、`query_features`（查询侧证据特征）、`gate`（feature gate 裁决：`applied` / `filtered` / `matched` / `conflicted`）。报告包含案例 ID；cloud_payload.json 可核对实际送入模型的参考案例。关闭 RAG 时输出与旧契约完全一致（仅 5 个 legacy 字段、cases 为空），且不添加 reference_cases。

## 案例库及边界

backend/app/rag/cases.json 是人工编写的 6 个教学案例，覆盖顶层列表、嵌套列表、别名映射、总数校验，以及尚不支持的游标和 offset 分页边界。它们不是从真实用户任务自动学习所得，也不是评测集。

只使用脱敏摘要的 query 和 response_shape 中的结构键做检索；不使用响应值、URL 或密钥。当前针对英文接口字段名分词，不是中文全文搜索。按正相关分数选至多 2 个案例，无匹配则不注入。相同分数按案例 ID 排序。案例只提供参考；原有本机地址、字段、页码、唯一键和完整性验证仍生效。添加案例不会扩展执行器支持范围。

## M5.1 feature gate（观测感知检索）

在 BM25 之上叠加一层确定性结构匹配，BM25 仍是排序主体：

- **确定性**：`match_features` 只做闭集枚举比较（exact match 加分、conflict 降权、unknown 不惩罚），无模糊推理、无字符串生成。
- **无模型调用**：不引入 embedding、向量数据库或 reranker，也不增加任何 LLM 调用。
- **additive 契约**：`retrieval.json` 的 legacy 字段不变，仅追加 `feature_schema_version` / `query_features` / `gate`；旧案例（无 `features`）与旧调用方继续正常工作。
- **BM25 兼容性保持**：冻结案例库不含 `features` 时 `gate.applied=false`，BM25 分数、排序与旧行为完全一致；关闭 RAG 时输出与旧契约逐字段相同。
- **安全边界**：`method` 属于硬边界，声明相反 method 的案例会被过滤而非降权，避免诱导 GET/POST 互转。

M5.1-C 提供确定性评估（tests/test_rag_evaluation.py）：在同一语料上对比 baseline BM25 与 BM25 + feature gate，只读冻结 benchmark，不改动案例库；评估集是仓库内小样本 fixture，不是 benchmark，结论不外推。

## M5.2 Failure-aware Knowledge Retrieval

M5.2 在 feature gate 之后追加一层确定性 failure-aware 排序，让案例从“文本相似样板”变为“结构化失败知识”。链路：

```
query / evidence
      │
      ▼
    BM25
      │
      ▼
 Feature Gate
      │
      ▼
Failure-aware Ranking
```

职责划分：

- `backend/app/rag/knowledge.py`：只读加载与 **schema validation**，并做 **repairability projection**（由 `errors.SPECS` + repair 只读策略常量确定性投影，案例只能声明、不能设定策略；不一致即 fail closed）。结构化知识只经 `knowledge.load()` 读取，不直接解析 JSON。
- `backend/app/rag/retrieval.py`：负责 **`failure_match`** 与 **deterministic ranking**。把观测到的失败（错误码 / 类别 / 阶段）与案例 `failure_modes` 做闭集比较：exact code 加权、category / phase 不匹配降权、unknown 中性。BM25 仍是排序主体。

安全边界：**SECURITY / DATA_INTEGRITY / TRANSPORT / UNCLASSIFIED** 这类受限失败下，不会推荐携带 `auto_applied` 知识的案例（直接过滤而非降权），因此**不会产生 auto repair**；`repair.py` 仍是可修复性的唯一权威。

依赖边界：无 **embedding**、无 **vector database**、无 **reranker**、无 **LLM** 调用。默认冻结语料（v1）不含 `failure_modes`，该阶段停用，`retrieval.json` 不会追加 `case_schema_version` / `knowledge_gate`，输出与 M5.1 逐字段一致。

确定性评估见 tests/test_rag_failure_evaluation.py（A/B/C 三组消融）与 [M5.2 评估报告](M5.2_EVALUATION_REPORT.md)；评估为小样本 fixture，不是 benchmark。

## M5.3 Runtime Failure-aware Diagnostic Retrieval

M5.3 把 failure-aware 检索从“能力层”接到失败分支的运行时：任务失败后，`run.py` 用失败分类做一次只读诊断型检索，并写入 additive 产物 `failure_retrieval.json`。failure flow：

```
errors.classify()
        ↓
  failure_context
        ↓
     retrieve()
        ↓
failure_retrieval.json
```

- **只读**：复用内存中已脱敏摘要，不重新观察、不改计划、不触达修复。
- **deterministic**：错误码 → 类别 / 阶段全部由 `errors.SPECS` 确定，调用方无法伪造；同输入结果 deep-equal。
- **no LLM call**：不新增任何模型调用（任务 `model_calls` 恒为 1）。
- **no repair influence**：`repair.py` 仍是可修复性的唯一权威；`repair.json` 不受影响，`retrieval.json` 契约不变。
- **fail closed**：`UNCLASSIFIED` 失败与关闭 RAG 时不产出诊断产物；成功任务也不产出。

评估见 [M5.3 评估报告](M5.3_EVALUATION_REPORT.md)（A/B/C/D 四组消融）。

案例库只从代码仓库固定路径加载，不接受网页内容或任务产物自动入库。修改案例需人工审查、更新 corpus_version，并重新测试；文件摘要可识别实际版本。当前小型库同步加载，后续扩大时再缓存或转数据库。

## 验证记录（2026-09-13）

- 新增检索排名、无匹配、禁用、响应值不参与、严格 API 开关以及开关到模型提示与产物的集成测试。
- 真实 API 任务 f7cd98f4-2514-4fb3-8931-3a44482faf34：命中 page_items、rows_more，30 条、3 页完整采集，5.05 秒，一次模型调用。
- 输入 890 tokens，输出 90 tokens；检索不额外调用模型，但注入案例会增加输入长度和可能的费用。
- 逐条核对固定商品数据，7 个下载产物的 SHA256 全部一致。

这只是连通性与固定靶场验证，不是 RAG 效果提升证据。下一步先实现 Vue 任务控制台，再准备冻结的多结构测试集，比较规则、无 RAG 模型和 RAG 模型的正确率、完整率、耗时与 token；开发案例不能直接当作独立测试样本。
