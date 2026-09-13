"""本机靶场 CLI：浏览器观察 → DeepSeek 分析 → 本地采集。"""

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
import uuid

import httpx
from playwright.async_api import Error as BrowserError

from ..llm.config import CloudConfig
from ..llm.gateway import CloudGateway, GatewayError
from ..rag.retrieval import retrieve
from .contracts import ExtractionPlan, cloud_summary, local_origin
from .execution import execute_plan
from .export import export_collector
from .observe import observe
from .curl_import import observe_imported


def save_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


async def run_task(args, config, *, task_id=None, output_root=None, on_stage=None, quiet=False):
    task_id = task_id or str(uuid.uuid4())
    # 注入 ID 由任务服务生成；对目录使用的 ID 再进行严格规范化。
    task_id = str(uuid.UUID(task_id))
    root = Path(output_root) if output_root is not None else Path(__file__).resolve().parents[3] / 'data'
    folder = root / 'tasks' / task_id
    folder.mkdir(parents=True)
    started = time.monotonic()
    gateway = CloudGateway(config)
    report = {'task_id': task_id, 'status': 'running', 'model': config.model}
    async def stage(name, message):
        if on_stage is not None:
            await on_stage(name)
        if not quiet:
            print(message, flush=True)
    try:
        async with asyncio.timeout(180):
            await stage('observing', '1/3 观察本机页面和翻页请求…')
            imported_url = getattr(args, 'imported_url', None)
            observations = await observe_imported(imported_url) if imported_url else await observe(args.url, args.click_text or None)
            report['source'] = 'curl_import' if imported_url else 'browser'
            # 持久化证据不写完整响应；本机执行仍使用内存中的实际样本校验。
            summaries = [cloud_summary(record) for record in observations[:5]]
            save_json(folder / 'evidence.json', summaries)
            fields = [name.strip() for name in args.fields.split(',') if name.strip()]
            retrieval = retrieve(summaries, enabled=getattr(args, 'rag_enabled', True))
            save_json(folder / 'retrieval.json', retrieval)
            report['retrieval'] = {key: value for key, value in retrieval.items() if key != 'cases'}
            report['retrieval']['case_ids'] = [case['id'] for case in retrieval['cases']]
            payload = {
                'task': 'Select a real request and map these requested fields for GET page-number collection. '
                        'Use JSON Pointers. Page numbers start at 1 and increment by 1. '
                        'Values in response_shape are type placeholders, not actual response values.',
                'requested_fields': fields, 'observations': summaries,
            }
            if retrieval['cases']:
                payload['reference_cases'] = retrieval['cases']
                payload['task'] += ' Reference cases are hints only; actual observations take precedence. Never invent paths or parameters from a case.'
            save_json(folder / 'cloud_payload.json', payload)
            await stage('analyzing', '2/3 请求 DeepSeek 生成采集计划（一次云调用）…')
            result = await gateway.generate(payload, ExtractionPlan)
            plan = result.data
            if set(plan.fields) != set(fields):
                raise ValueError('requested_fields_mismatch')
            save_json(folder / 'plan.json', plan.model_dump())
            await stage('executing', '3/3 校验计划并执行本地分页采集…')
            async with httpx.AsyncClient(trust_env=False) as client:
                collected = await execute_plan(client, plan, observations, args.max_pages)
            await stage('verifying', '写入已验证的采集结果…')
            save_json(folder / 'result.json', collected['items'])
            report.update(status='succeeded', count=len(collected['items']), pages=collected['pages'],
                          completeness=collected['completeness'], expected_total=collected['expected_total'])
        # 核心采集成功后再导出独立脚本；导出/执行/比对失败只记录状态，不改变已成功的任务结果。
        try:
            chosen = next((item for item in observations if item.request_id == plan.request_id), None)
            report.update(await export_collector(folder, plan, chosen, collected, args.max_pages))
        except Exception:
            report['collector_exported'] = False
            report['collector_execution_success'] = False
            report['collector_matches_internal_result'] = False
    except GatewayError as error:
        report.update(status='failed', error=error.code)
    except (ValueError, httpx.HTTPError, BrowserError, TimeoutError, OSError) as error:
        # 不把网络异常 URL、验证错误输入或供应商正文写入报告。
        report.update(status='failed', error=type(error).__name__)
    except asyncio.CancelledError:
        report.update(status='cancelled', error='cancelled')
        raise
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['model_calls'] = gateway.calls
        report['usage'] = [asdict(u) for u in gateway.usage_history]
        save_json(folder / 'report.json', report)
        (folder / 'report.md').write_text(
            '# 本机测试站分析报告\n\n'
            + '\n'.join(f'- {key}: {value}' for key, value in report.items())
            + '\n\n范围：本机 GET 页码分页；支持可关闭的本地案例 RAG；成功任务另导出独立 collector.py。\n', encoding='utf-8')
    if not quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f'产物目录：{folder}')
    return 0 if report['status'] == 'succeeded' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000/')
    parser.add_argument('--fields', default='id,name,price_fen')
    parser.add_argument('--click-text', default='下一页', help='观察时点击的按钮文字；传空字符串跳过')
    parser.add_argument('--max-pages', type=int, default=3)
    parser.add_argument('--no-rag', dest='rag_enabled', action='store_false', help='关闭案例检索，用于对照实验')
    args = parser.parse_args()
    try:
        local_origin(args.url)
        if not 1 <= args.max_pages <= 10 or not 1 <= len(args.fields) <= 200:
            raise ValueError()
        config = CloudConfig.deepseek_from_env()
    except ValueError:
        parser.exit(1, '配置无效：请设置 DEEPSEEK_API_KEY，使用本机 HTTP URL，并限制页数为 1～10。\n')
    try:
        parser.exit(asyncio.run(run_task(args, config)))
    except KeyboardInterrupt:
        parser.exit(130, '任务已取消。\n')


if __name__ == '__main__':
    main()
