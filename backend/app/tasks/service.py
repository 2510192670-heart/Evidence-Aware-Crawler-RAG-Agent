import asyncio
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from ..pipeline.run import run_task, save_json
from ..pipeline.curl_import import checked_url
from ..policy import TargetPolicy, TargetPolicyError, default_policy, policy_sha256
from ..policy.data_policy import admit_intent
from ..policy.enforcement import check_target as enforce_check_target
from ..storage.repository import BusyError, TERMINAL

# policy 引用只有一种合法形态：已注册策略的内容寻址 sha256（小写 hex）。
POLICY_REFERENCE_PATTERN = re.compile(r'[0-9a-f]{64}')


def resolve_policy(reference, policies=()):
    """policy 引用 → TargetPolicy；来源必须是服务端可信注册表。

    None 表示默认 loopback 策略（旧任务行为不变）。前端不能构造规则：请求只能
    携带部署侧预注册策略的 sha256 引用，本函数是策略解析的唯一闸门，非法引用
    与未注册引用分别以稳定码拒绝（fail-closed，未注册进 SPECS 前 classify 为
    UNCLASSIFIED）。
    """
    if reference is None:
        return default_policy()
    if not isinstance(reference, str) or not POLICY_REFERENCE_PATTERN.fullmatch(reference):
        raise TargetPolicyError('invalid_policy_reference')
    for policy in policies:
        if policy_sha256(policy) == reference:
            return policy
    raise TargetPolicyError('policy_not_found')


async def pipeline_worker(spec, config, task_id, root, stage):
    args = SimpleNamespace(url=spec['url'], fields=','.join(spec['fields']),
                           description=spec.get('description', ''), field_specs=spec.get('field_specs', []),
                           max_records=spec.get('max_records'),
                           preview=spec.get('preview', False),
                           source_mode=spec.get('source_mode', 'json'),
                           max_pages=spec['max_pages'], click_text=spec['click_text'],
                           rag_enabled=spec.get('rag_enabled', True), imported_url=spec.get('imported_url'),
                           imported_method=spec.get('imported_method', 'GET'),
                           imported_request_body=spec.get('imported_request_body'),
                           purpose=spec.get('purpose'),
                           # submit() 已把服务端解析出的策略注入 spec；缺省回落 loopback。
                           policy=TargetPolicy(**spec['policy']) if spec.get('policy') else None)
    options = {'on_preview': getattr(stage, 'preview', None)} if args.preview else {}
    await run_task(args, config, task_id=task_id, output_root=root, on_stage=stage, quiet=True, **options)
    return json.loads((root / 'tasks' / task_id / 'report.json').read_text(encoding='utf-8'))


class TaskService:
    def __init__(self, repository, config_factory, worker=pipeline_worker, policies=(), model_profiles=None):
        self.repo = repository
        self.config_factory = config_factory
        self.model_profiles = model_profiles
        self.worker = worker
        # 服务端可信策略注册表（部署配置构造，非请求数据）。
        self.policies = tuple(policies)
        self.handles = {}
        self.previews = {}
        self.lock = asyncio.Lock()

    async def submit(self, spec):
        async with self.lock:
            if any(not task.done() for task in self.handles.values()):
                raise BusyError('task_already_running')
            # M7 Governance admission 顺序：UserIntent → DataPolicy → TargetPolicy。
            # DataPolicy 只判定"是否应该采集这种数据"，不看 URL；拒绝时不落库、零 IO。
            decision = admit_intent(spec.get('description', ''), spec.get('purpose'),
                                    spec.get('fields', ()))
            policy = resolve_policy(spec.get('policy_sha256'), self.policies)
            try:
                # S3.2-B1 任务创建 admission：唯一权威目标判定，只调用 enforcement
                # 层（委托单一真源），此处不复制任何 allowlist/scheme/IP 规则。
                enforce_check_target(spec['url'], policy)
                if spec.get('imported_url'):
                    checked_url(spec['imported_url'], policy)
                    # Public imports must belong to the selected entry origin;
                    # trusting the preview would allow a crafted task to bypass admission.
                    if policy.mode == 'public_http':
                        entry, imported = urlsplit(spec['url']), urlsplit(spec['imported_url'])
                        if (entry.scheme, entry.netloc) != (imported.scheme, imported.netloc):
                            raise TargetPolicyError('cross_origin_request')
            except TargetPolicyError:
                raise
            except ValueError as error:
                # loopback 委托产生的旧码字符串（如 only_literal_loopback_http_supported）：
                # 码原样保留，仅在 API 边界包装进策略错误通道以便稳定映射。
                raise TargetPolicyError(str(error)) from None
            # 规范化策略与治理决策随 spec 持久化（tasks.spec JSON 列），形成任务级审计链。
            spec = {**spec, 'policy': policy.model_dump(mode='json'),
                    'data_policy': decision.model_dump(mode='json')}
            config = (self.model_profiles.resolve(spec['model_profile'])
                      if spec.get('model_profile') and self.model_profiles is not None else self.config_factory())
            row = await asyncio.to_thread(self.repo.create, spec, config.model)
            task_id = row['id']
            handle = asyncio.create_task(self._drive(row, config))
            self.handles[task_id] = handle
            handle.add_done_callback(lambda task: self.handles.pop(task_id, None))
            return row

    def read_report(self, task_id, fallback):
        path = self.repo.root / 'tasks' / task_id / 'report.json'
        try:
            content = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(content, dict):
                return content
        except (ValueError, OSError):
            pass
        return fallback

    def store_final_report(self, task_id, report):
        folder = self.repo.root / 'tasks' / task_id
        folder.mkdir(parents=True, exist_ok=True)
        save_json(folder / 'report.json', report)
        # API 终态为事实来源；取消竞态下将文件状态与数据库同步。
        (folder / 'report.md').write_text('# 任务报告\n\n' + '\n'.join(
            f'- {key}: {value}' for key, value in report.items()) + '\n', encoding='utf-8')

    async def _drive(self, row, config):
        task_id = row['id']
        async def stage(name):
            await asyncio.to_thread(self.repo.transition, task_id, name)
        async def preview(value):
            future = asyncio.get_running_loop().create_future()
            self.previews[task_id] = (value['plan_hash'], future)
            try:
                await asyncio.to_thread(self.repo.update_preview, task_id,
                                        {**value, 'status': 'awaiting_confirmation'})
                await asyncio.wait_for(future, timeout=120)
            finally:
                self.previews.pop(task_id, None)
        stage.preview = preview
        try:
            report = await self.worker(row['spec'], config, task_id, self.repo.root, stage)
            if not isinstance(report, dict) or report.get('status') not in TERMINAL:
                report = {'status': 'failed', 'error': 'invalid_worker_report'}
        except asyncio.CancelledError:
            report = self.read_report(task_id, {})
            report.update(status='cancelled', error='cancelled')
        except Exception:
            report = {'status': 'failed', 'error': 'worker_failed'}
        # 最后提交与 cancel 使用同一锁，避免一边登记成功一边重写取消报告。
        async with self.lock:
            try:
                current = await asyncio.to_thread(self.repo.get, task_id)
                if current['status'] == 'cancelling':
                    report.update(status='cancelled', error='cancelled')
                await asyncio.to_thread(self.store_final_report, task_id, report)
                await asyncio.to_thread(self.repo.register_artifacts, task_id)
                await asyncio.to_thread(self.repo.transition, task_id, report['status'], report)
            except Exception:
                await asyncio.to_thread(self.repo.transition, task_id, 'failed',
                                        {'status': 'failed', 'error': 'artifact_or_storage_failed'})

    async def confirm(self, task_id, plan_hash):
        async with self.lock:
            pending = self.previews.get(task_id)
            row = await asyncio.to_thread(self.repo.get, task_id)
            if (not pending or pending[0] != plan_hash or pending[1].done()
                    or row is None or row['status'] != 'analyzing'):
                return False
            pending[1].set_result(True)
            return True

    async def cancel(self, task_id):
        async with self.lock:
            row = await asyncio.to_thread(self.repo.get, task_id)
            if row is None or row['status'] in TERMINAL:
                return row
            await asyncio.to_thread(self.repo.transition, task_id, 'cancelling')
            handle = self.handles.get(task_id)
            if handle is not None and not handle.done():
                handle.cancel()
        if handle is not None:
            await asyncio.gather(handle, return_exceptions=True)
        async with self.lock:
            # 即使协程在第一次执行前被取消，也确保终态落库。
            current = await asyncio.to_thread(self.repo.get, task_id)
            if current['status'] not in TERMINAL:
                report = self.read_report(task_id, {})
                report.update(status='cancelled', error='cancelled')
                await asyncio.to_thread(self.store_final_report, task_id, report)
                await asyncio.to_thread(self.repo.register_artifacts, task_id)
                await asyncio.to_thread(self.repo.transition, task_id, 'cancelled', report)
            return await asyncio.to_thread(self.repo.get, task_id)

    async def shutdown(self):
        for task_id in list(self.handles):
            await self.cancel(task_id)
