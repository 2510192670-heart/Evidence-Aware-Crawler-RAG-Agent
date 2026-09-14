# 本机测试站分析报告

- task_id: ffe4f6bb-4b25-5a4d-92de-f1ea798a7ff2
- status: succeeded
- model: fixture-planner-stub
- source: browser
- retrieval: {'enabled': True, 'algorithm': 'BM25Okapi', 'corpus_version': 1, 'corpus_sha256': '678f87ece8bdb0aa4171f5f80b6b289cd773215639b7e30d82dc4953f5cba1f2', 'feature_schema_version': 1, 'query_features': {'method': 'GET', 'page_location': 'query', 'items_container': 'nested', 'stop_signal': 'total_int_only', 'continuation_flag': 'absent', 'field_mapping': 'unknown', 'ambiguity': 'single_candidate'}, 'gate': {'applied': False, 'filtered': [], 'matched': [], 'conflicted': []}, 'case_ids': ['page_items', 'rows_more']}
- count: 1
- pages: 1
- completeness: complete
- expected_total: 1
- collector_exported: False
- collector_execution_success: False
- collector_matches_internal_result: False
- repair_attempted: True
- repair_count: 1
- repair_outcome: applied
- repair_applied: True
- repair_original_plan_hash: 7f084e9e55ac7aeb84ae402bb0920bf53d993dcdde252a32ba170f3b04d37563
- repair_candidate_plan_hash: 9c9ad0a8eef032e07d250f3f0b098807d905c0975dfaeafc886365c964d56d9e
- repair_execution_plan_hash: 9c9ad0a8eef032e07d250f3f0b098807d905c0975dfaeafc886365c964d56d9e
- elapsed_seconds: 0.0
- model_calls: 1
- usage: []

范围：本机 GET 页码分页；支持可关闭的本地案例 RAG；成功任务另导出独立 collector.py。
