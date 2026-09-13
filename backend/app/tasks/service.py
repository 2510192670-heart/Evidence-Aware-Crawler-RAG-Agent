import asyncio
import json
from types import SimpleNamespace

from ..pipeline.run import run_task, save_json
from ..storage.repository import BusyError, TERMINAL


async def pipeline_worker(spec, config, task_id, root, stage):
    args = SimpleNamespace(url=spec['url'], fields=','.join(spec['fields']),
                           max_pages=spec['max_pages'], click_text=spec['click_text'],
                           rag_enabled=spec.get('rag_enabled', True), imported_url=spec.get('imported_url'))
    await run_task(args, config, task_id=task_id, output_root=root, on_stage=stage, quiet=True)
    return json.loads((root / 'tasks' / task_id / 'report.json').read_text(encoding='utf-8'))


class TaskService:
    def __init__(self, repository, config_factory, worker=pipeline_worker):
        self.repo = repository
        self.config_factory = config_factory
        self.worker = worker
        self.handles = {}
        self.lock = asyncio.Lock()

    async def submit(self, spec):
        async with self.lock:
            if any(not task.done() for task in self.handles.values()):
                raise BusyError('task_already_running')
            config = self.config_factory()
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
