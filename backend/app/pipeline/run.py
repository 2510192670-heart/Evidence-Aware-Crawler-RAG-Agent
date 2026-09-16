"""本机靶场 CLI：浏览器观察 → DeepSeek 分析 → 本地采集。"""

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import httpx
from playwright.async_api import Error as BrowserError

from ..llm.config import CloudConfig
from ..llm.gateway import CloudGateway, GatewayError
from ..policy import default_policy
from ..policy.enforcement import check_target as policy_check_target
from ..policy.resolution import resolve_and_classify
from ..policy.robots import RobotsPolicy
from ..policy.transport import build_public_transport
from ..rag.failure import build_failure_context, build_failure_retrieval
from ..rag.retrieval import retrieve
from .contracts import ExtractionPlan, cloud_summary, local_origin, plan_hash
from .errors import PipelineError, classify
from .execution import execute_plan
from .export import export_collector
from .observe import observe
from .curl_import import checked_url, observe_imported
from .repair import (MAX_REPAIR_ATTEMPTS, ProposalOutcome, RepairApplication, RepairContext,
                     RepairProposer, audit_document, evaluate_failure, is_applicable)


def save_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


ROBOTS_MAX_BYTES = 64 * 1024


def admit_public_entry(url: str, policy, *, imported: bool) -> str:
    """公网入口目标判定（admission 序列第 1 步），返回规范化 origin。

    浏览器入口走 check_target（保留 entry-query 限制）；导入入口走 curl_import
    的 checked_url（query 是页码证据，凭证/fragment 照旧拒绝）。判定全部委托
    enforcement 单一真源，本函数不复制任何规则。
    """
    if imported:
        checked_url(url, policy)
    else:
        policy_check_target(url, policy)
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


async def fetch_robots(origin: str, transport) -> RobotsPolicy:
    """抓取 robots.txt：经守卫 transport（每请求重解析分类）；fail-closed。

    404 = 无 robots 文件 = 无规则（RFC 9309 惯例，允许）；其他非 200、网络
    异常、非法文本一律拒绝。安全类异常（如 ssrf_ip_blocked）不在此捕获，
    以稳定码上抛。
    """
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     transport=transport) as client:
            response = await client.get(origin + '/robots.txt', timeout=10)
    except (httpx.HTTPError, OSError):
        return RobotsPolicy()
    if response.status_code == 404:
        return RobotsPolicy('', fetch_succeeded=True)
    if response.status_code != 200:
        return RobotsPolicy()
    try:
        return RobotsPolicy(response.content[:ROBOTS_MAX_BYTES].decode('utf-8'),
                            fetch_succeeded=True)
    except UnicodeDecodeError:
        return RobotsPolicy()


def failure_fields(error) -> dict:
    """稳定的失败分类字段；不写入异常消息、目标 URL 或供应商正文。"""
    classification = classify(error)
    return {'error': classification.code,
            'error_type': type(error).__name__,
            'error_category': classification.category.value,
            'error_repairable': classification.repairable,
            'error_retryable': classification.retryable}


def finalize_repair_fields(report, repair_state):
    """写入 repair 谱系字段；旧字段保持兼容，新增字段始终存在。"""
    repaired = repair_state['repaired']
    proposals, attempts = repair_state['proposals'], repair_state['attempts']
    if repaired:
        outcome = 'applied' if report.get('status') == 'succeeded' else 'applied_failed'
    elif proposals and proposals[-1].outcome == ProposalOutcome.PROPOSED.value:
        # 有有效候选但不在自动应用白名单内，只审计不执行。
        outcome = 'deferred'
    elif attempts:
        outcome = 'rejected'
    else:
        outcome = None
    report.update(repair_attempted=repaired > 0, repair_count=repaired, repair_outcome=outcome,
                  repair_applied=repair_state['applied'],
                  repair_original_plan_hash=repair_state['original_plan_hash'],
                  repair_candidate_plan_hash=repair_state['candidate_plan_hash'],
                  repair_execution_plan_hash=repair_state['execution_plan_hash'])


async def execute_with_bounded_repair(client, plan, observations, max_pages, record, repair_state,
                                      proposer=None, policy=None):
    """有界执行修复（M4.4.3）：候选最多额外执行 MAX_REPAIR_ATTEMPTS 次。

    原计划、候选计划、执行计划始终是三个独立对象：本函数只把局部变量指向候选，
    绝不修改或覆盖传入的 ``plan``。返回实际用于执行的计划与采集结果；不可修复的
    失败原样抛出，交由上层的失败分类处理。
    """
    proposer = proposer or RepairProposer()
    original_hash = plan_hash(plan)
    repair_state['original_plan_hash'] = original_hash
    execution_plan = plan
    while True:
        try:
            if policy is None:
                # loopback：调用形态与接线前逐字一致（兼容既有执行桩）。
                collected = await execute_plan(client, execution_plan, observations, max_pages)
            else:
                collected = await execute_plan(client, execution_plan, observations, max_pages,
                                               policy=policy)
        except ValueError as error:
            attempt, proposal, candidate = evaluate_failure(
                error, RepairContext(original_plan_hash=original_hash, attempts=repair_state['repaired']),
                plan, record, proposer=proposer)
            repair_state['attempts'].append(attempt)
            if proposal is not None:
                repair_state['proposals'].append(proposal)
            # 三重闸门：必须有候选、必须命中自动应用白名单、必须还有修复预算。
            if (candidate is None or not is_applicable(error, proposal)
                    or repair_state['repaired'] >= MAX_REPAIR_ATTEMPTS):
                repair_state['audited_error'] = error
                repair_state['execution_plan_hash'] = plan_hash(execution_plan)
                repair_state['execution_result'] = {'status': 'failed', 'error': attempt.error_code}
                raise
            execution_plan = candidate          # 只改变量绑定，原计划对象不动
            repair_state['candidate_plan_hash'] = proposal.candidate_plan_hash
            repair_state['applied'] = True
            repair_state['repaired'] += 1
            continue
        except Exception as error:
            # 非计划类失败（网络等）不参与修复，但执行确实发生过。
            repair_state['execution_plan_hash'] = plan_hash(execution_plan)
            repair_state['execution_result'] = {'status': 'failed', 'error': type(error).__name__}
            raise
        repair_state['execution_plan_hash'] = plan_hash(execution_plan)
        repair_state['execution_result'] = {'status': 'succeeded', 'count': len(collected['items']),
                                            'pages': collected['pages'],
                                            'completeness': collected['completeness']}
        return execution_plan, collected


async def run_task(args, config, *, task_id=None, output_root=None, on_stage=None, quiet=False,
                   policy=None):
    # M7-A S3.1：策略只来自服务端可信注册表（显式 kwarg 或 spec 注入的 args.policy）；
    # 两者都缺省时回落默认 loopback，既有调用方行为逐字节不变。
    policy = policy if policy is not None else getattr(args, 'policy', None)
    if policy is None:
        policy = default_policy()
    task_id = task_id or str(uuid.uuid4())
    # 注入 ID 由任务服务生成；对目录使用的 ID 再进行严格规范化。
    task_id = str(uuid.UUID(task_id))
    root = Path(output_root) if output_root is not None else Path(__file__).resolve().parents[3] / 'data'
    folder = root / 'tasks' / task_id
    folder.mkdir(parents=True)
    started = time.monotonic()
    gateway = CloudGateway(config)
    # repair_* 是稳定契约；repair_state 汇总本次任务的全部 repair 审计与谱系。
    report = {'task_id': task_id, 'status': 'running', 'model': config.model}
    plan = None
    execution_plan = None
    observations = []
    summaries = None
    repair_state = {'attempts': [], 'proposals': [], 'original_plan_hash': None,
                    'candidate_plan_hash': None, 'execution_plan_hash': None,
                    'applied': False, 'repaired': 0, 'execution_result': None, 'audited_error': None}
    async def stage(name, message):
        if on_stage is not None:
            await on_stage(name)
        if not quiet:
            print(message, flush=True)

    def plan_record():
        return next((item for item in observations
                     if plan is not None and item.request_id == plan.request_id), None)

    def record_failure_retrieval(error):
        """M5.3-A：失败分支的诊断型 re-retrieval（只读、无模型调用）。

        复用内存中已有的脱敏摘要，不重新观察、不重新规划、不触达修复。结果只写入
        additive 产物 ``failure_retrieval.json`` 作为解释证据，绝不回灌规划或修复。
        """
        if summaries is None or not getattr(args, 'rag_enabled', True):
            return
        try:
            failure_context = build_failure_context(error)
            if failure_context is None:
                return
            result = retrieve(summaries, enabled=True, failure_context=failure_context)
            artifact = build_failure_retrieval(error, result)
            if artifact is None:
                return
            save_json(folder / 'failure_retrieval.json', artifact)
        except (OSError, ValueError):
            # 诊断产物是补充证据：写入失败不得改变已确定的失败分类。
            return
        report['failure_retrieval'] = {'case_ids': artifact['case_ids'],
                                       'knowledge_gate_applied': artifact['knowledge_gate']['applied']}

    def record_failure(error):
        """记录失败分类；有界修复驱动已审计过的失败不重复审计。"""
        record_failure_retrieval(error)
        report.update(status='failed', **failure_fields(error))
        if repair_state['audited_error'] is error:
            return
        original_hash = repair_state['original_plan_hash'] or (plan_hash(plan) if plan is not None else None)
        repair_state['original_plan_hash'] = original_hash
        attempt, proposal, _ = evaluate_failure(
            error, RepairContext(original_plan_hash=original_hash, attempts=repair_state['repaired']),
            plan, plan_record())
        repair_state['attempts'].append(attempt)
        if proposal is not None:
            repair_state['proposals'].append(proposal)

    try:
        async with asyncio.timeout(180):
            await stage('observing', '1/3 观察本机页面和翻页请求…')
            imported_url = getattr(args, 'imported_url', None)
            resolver = getattr(args, 'resolver', None)
            transport = None
            robots = None
            if policy.mode == 'public_http':
                # C3-B 公网 admission 序列（固定顺序，任一步失败即 fail-closed）：
                # check_target → resolve_and_classify → transport → robots →
                # observe → execute。loopback 分支不进入本块，行为与既往逐字节一致。
                entry_url = imported_url or args.url
                entry_origin = admit_public_entry(entry_url, policy, imported=bool(imported_url))
                resolve_and_classify(urlsplit(entry_url).hostname, resolver)
                transport = build_public_transport(resolver=resolver)
                robots = await fetch_robots(entry_origin, transport)
                robots.enforce(urlsplit(entry_url).path or '/')
            # 等价性原则：loopback 分支的调用形态与接线前逐字一致（不传新 kwarg），
            # 公网分支才使用扩展签名。这保证既有行为与既有测试桩零漂移。
            if imported_url:
                # 直接复用 curl_import 已解析的请求元数据重放，不再次解析 cURL 文本。
                if transport is not None:
                    observations = await observe_imported(
                        imported_url, getattr(args, 'imported_method', 'GET'),
                        getattr(args, 'imported_request_body', None),
                        policy=policy, transport=transport)
                else:
                    observations = await observe_imported(
                        imported_url, getattr(args, 'imported_method', 'GET'),
                        getattr(args, 'imported_request_body', None))
            else:
                if policy.mode == 'public_http':
                    observations = await observe(args.url, args.click_text or None,
                                                 policy=policy, resolver=resolver)
                else:
                    observations = await observe(args.url, args.click_text or None)
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
                'task': 'Select a real request and map these requested fields for page-number collection. '
                        'Use JSON Pointers. Page numbers start at 1 and increment by 1. '
                        'Set pagination_location to the observed page-number location: '
                        '"query" for GET, "json_body" for a POST JSON body. '
                        'Values in response_shape or request_body_shape are type placeholders, not actual response values.',
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
            if robots is not None:
                execution_record = plan_record()
                if execution_record is not None:
                    # 执行目标 path 也须过 robots 判定（可能与入口 path 不同）。
                    robots.enforce(urlsplit(execution_record.url).path or '/')
            # loopback（transport=None）时构造参数与旧代码逐字一致；公网才注入受守卫 transport。
            client_kwargs = {'trust_env': False}
            if transport is not None:
                client_kwargs['transport'] = transport
            async with httpx.AsyncClient(**client_kwargs) as client:
                execution_plan, collected = await execute_with_bounded_repair(
                    client, plan, observations, args.max_pages, plan_record(), repair_state,
                    # loopback 传 None：execute_plan 保持旧调用形态（等价性原则）。
                    policy=policy if policy.mode == 'public_http' else None)
            await stage('verifying', '写入已验证的采集结果…')
            save_json(folder / 'result.json', collected['items'])
            report.update(status='succeeded', count=len(collected['items']), pages=collected['pages'],
                          completeness=collected['completeness'], expected_total=collected['expected_total'])
        # 核心采集成功后再导出独立脚本；导出/执行/比对失败只记录状态，不改变已成功的任务结果。
        try:
            # 用真正执行过的计划导出，修复后的任务其 collector 才与内部结果一致。
            chosen = next((item for item in observations if item.request_id == execution_plan.request_id), None)
            report.update(await export_collector(folder, execution_plan, chosen, collected, args.max_pages))
        except Exception:
            report['collector_exported'] = False
            report['collector_execution_success'] = False
            report['collector_matches_internal_result'] = False
    except GatewayError as error:
        record_failure(error)
    except PipelineError as error:
        # 必须排在 ValueError 之前：PipelineError 是 ValueError 子类，顺序颠倒会丢失分类。
        record_failure(error)
    except (ValueError, httpx.HTTPError, BrowserError, TimeoutError, OSError) as error:
        # 不把网络异常 URL、验证错误输入或供应商正文写入报告。
        record_failure(error)
    except asyncio.CancelledError:
        report.update(status='cancelled', error='cancelled')
        raise
    except Exception as error:
        record_failure(error)
    finally:
        finalize_repair_fields(report, repair_state)
        # 审计产物是补充证据：写入失败不应改变已确定的失败分类。
        if repair_state['attempts']:
            application = RepairApplication(
                original_plan_hash=repair_state['original_plan_hash'],
                candidate_plan_hash=repair_state['candidate_plan_hash'],
                applied=repair_state['applied'],
                execution_result=repair_state['execution_result'])
            try:
                save_json(folder / 'repair.json',
                          audit_document(repair_state['attempts'], repair_state['proposals'], application))
            except OSError:
                pass
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
