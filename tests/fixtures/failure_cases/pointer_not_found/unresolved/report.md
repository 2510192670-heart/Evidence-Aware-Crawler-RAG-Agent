# 本机测试站分析报告

- task_id: 39760fd4-d5b5-5090-9ac1-c687f9b4b20d
- status: failed
- model: fixture-planner-stub
- source: browser
- retrieval: {'enabled': True, 'algorithm': 'BM25Okapi', 'corpus_version': 1, 'corpus_sha256': '678f87ece8bdb0aa4171f5f80b6b289cd773215639b7e30d82dc4953f5cba1f2', 'feature_schema_version': 1, 'query_features': {'method': 'GET', 'page_location': 'query', 'items_container': 'nested', 'stop_signal': 'total_int_only', 'continuation_flag': 'absent', 'field_mapping': 'unknown', 'ambiguity': 'multiple_candidates'}, 'gate': {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}, 'case_ids': ['page_items', 'rows_more']}
- failure_retrieval: {'case_ids': ['page_items', 'rows_more'], 'knowledge_gate_applied': False}
- error: pointer_not_found
- error_type: PipelineError
- error_category: PLAN_SEMANTIC
- error_repairable: True
- error_retryable: False
- repair_attempted: False
- repair_count: 0
- repair_outcome: rejected
- repair_applied: False
- repair_original_plan_hash: 7f084e9e55ac7aeb84ae402bb0920bf53d993dcdde252a32ba170f3b04d37563
- repair_candidate_plan_hash: None
- repair_execution_plan_hash: 7f084e9e55ac7aeb84ae402bb0920bf53d993dcdde252a32ba170f3b04d37563
- elapsed_seconds: 0.0
- model_calls: 1
- usage: []

范围：本机 GET 页码分页；支持可关闭的本地案例 RAG；成功任务另导出独立 collector.py。
