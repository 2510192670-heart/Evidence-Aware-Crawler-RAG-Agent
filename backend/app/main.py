"""任务 API。启动：python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .llm.config import CloudConfig
from .llm.profiles import ModelProfiles, ProfileInput, ModelProfileError
from .llm.gateway import GatewayError
from .pipeline.contracts import local_origin, has_sensitive_keys, SENSITIVE
from .pipeline.curl_import import parse_curl, checked_url
from .pipeline.requirements import FieldSpec, safe_description
from .pipeline.result_export import export_csv, export_xlsx
from .policy import TargetPolicyError, policy_sha256
from .policy.data_policy import PURPOSES, DataPolicyError
from .policy.loader import load_policy_file
from .storage.instance_lock import InstanceLock
from .storage.repository import Repository, BusyError, TERMINAL, ARTIFACT_NAMES
from .tasks.service import TaskService, pipeline_worker, resolve_policy
from .trace.projection import project_trace

# 内联只读查看的白名单，从冻结的 ARTIFACT_NAMES 派生，避免第二份清单漂移。只有 JSON
# 契约产物可内联；collector.py / report.md 保持 download-only，不提供任何执行或富文本面。
INLINE_ARTIFACTS = frozenset(name for name in ARTIFACT_NAMES if name.endswith('.json'))


class TaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    url: str = Field(default='http://127.0.0.1:8000/', max_length=512)
    fields: list[str] = Field(default_factory=lambda: ['id', 'name', 'price_fen'], min_length=1, max_length=10)
    description: str = Field(default='', max_length=2000)
    # M7 Governance：任务用途声明（封闭词汇，单一真源 data_policy.PURPOSES），
    # 随 spec 持久化并进入 DataPolicy 治理决策，形成可追溯审计链。
    purpose: str | None = Field(default=None, max_length=40)
    field_specs: list[FieldSpec] = Field(default_factory=list, max_length=10)
    max_records: int | None = Field(default=None, ge=1, le=1000)
    model_profile: str | None = Field(default=None, max_length=80)
    preview: bool = False
    source_mode: Literal['json', 'auto', 'html'] = 'json'
    max_pages: int = Field(default=3, ge=1, le=10)
    click_text: str = Field(default='下一页', max_length=80)
    rag_enabled: bool = True
    imported_url: str | None = Field(default=None, max_length=512)
    # 复用 curl_import 已解析的请求元数据；不再二次解析原始 cURL 文本。
    imported_method: str = Field(default='GET', max_length=8)
    imported_request_body: dict | None = Field(default=None)
    # M7-A S3.1：只允许引用服务端预注册策略的内容寻址 sha256；None 表示 loopback。
    # 用户不能直接提交 policy 对象（extra='forbid' 拒绝任何未声明键，含 'policy'）。
    policy_sha256: str | None = Field(default=None, max_length=64)

    @field_validator('description')
    @classmethod
    def check_description(cls, value):
        return safe_description(value)

    @field_validator('purpose')
    @classmethod
    def check_purpose(cls, value):
        if value is not None and value not in PURPOSES:
            raise ValueError('unsupported_purpose')
        return value

    @model_validator(mode='after')
    def check_field_specs(self):
        names = [spec.name for spec in self.field_specs]
        if names and (len(names) != len(set(names)) or set(names) != set(self.fields)):
            raise ValueError('field_specs_mismatch')
        return self

    @model_validator(mode='after')
    def check_imported_request(self):
        """导入元数据必须与 imported_url 同时出现，且方法与请求体形态一致。"""
        if self.imported_url is not None and self.policy_sha256 is None:
            checked_url(self.imported_url)
        if self.imported_url is None:
            if self.imported_method != 'GET' or self.imported_request_body is not None:
                raise ValueError('imported_request_requires_url')
            return self
        if self.imported_method not in {'GET', 'POST'}:
            raise ValueError('unsupported_imported_method')
        if self.imported_method == 'GET':
            if self.imported_request_body is not None:
                raise ValueError('get_import_must_not_carry_request_body')
        elif not isinstance(self.imported_request_body, dict) or not self.imported_request_body:
            raise ValueError('post_import_requires_json_object_body')
        elif has_sensitive_keys(self.imported_request_body):
            raise ValueError('sensitive_imported_body')
        return self

    @model_validator(mode='after')
    def check_url(self):
        """入口 URL 准入（S3.2-B1）。

        无 policy 引用时保持旧的 loopback-only 判定，行为与字节级冻结前完全一致；
        携带 policy 引用时只做结构预检（scheme/主机形态），权威目标判定在服务端
        admission（service.submit → enforcement.check_target）。本校验器不复制
        allowlist/端口/IP 任何规则——全系统只有一套目标判定。
        """
        if self.policy_sha256 is None:
            local_origin(self.url)
        else:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                raise ValueError('invalid_url')
        if urlsplit(self.url).query:
            raise ValueError('entry_query_not_supported')
        return self

    @field_validator('fields')
    @classmethod
    def check_fields(cls, values):
        if len(set(values)) != len(values) or any(
            not re.fullmatch(r'\w{1,40}', value) or SENSITIVE.search(value) for value in values
        ):
            raise ValueError('invalid_fields')
        return values


class APIError(Exception):
    def __init__(self, status, code, task_id=None, reason=None):
        self.status, self.code, self.task_id, self.reason = status, code, task_id, reason


class CurlInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(max_length=16384)
    policy_sha256: str | None = Field(default=None, max_length=64)


class ConfirmationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    plan_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


def error_body(code, task_id=None):
    return {'code': code, 'message': code, 'task_id': task_id, 'retryable': False}


def create_app(data_dir=None, config_factory=CloudConfig.deepseek_from_env, worker=pipeline_worker, console_directory=None,
               policies=None):
    root = Path(data_dir).resolve() if data_dir is not None else Path(__file__).resolve().parents[2] / 'data'

    @asynccontextmanager
    async def lifespan(app):
        instance_lock = InstanceLock(root)
        repo = Repository(root)
        try:
            await asyncio.to_thread(repo.initialize)
            await asyncio.to_thread(repo.recover)
            # policies 是部署级可信注册表；请求侧只能以 sha256 引用其中条目。
            deployed_policies = policies
            if deployed_policies is None:
                policy_path = os.environ.get('WDA_TARGET_POLICY_FILE')
                deployed_policies = (load_policy_file(policy_path),) if policy_path else ()
            profiles = ModelProfiles()
            service = TaskService(repo, config_factory, worker, policies=deployed_policies, model_profiles=profiles)
            app.state.model_profiles = profiles
            app.state.repo, app.state.service = repo, service
            try:
                yield
            finally:
                await service.shutdown()
                profiles.clear()
        finally:
            repo.close()
            instance_lock.close()

    app = FastAPI(title='Web Data Agent API', version='0.1.0', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])

    @app.middleware('http')
    async def origin_guard(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse(error_body('cross_origin_rejected'), status_code=403)
        return await call_next(request)

    @app.exception_handler(APIError)
    async def api_error(request, error):
        body = error_body(error.code, error.task_id)
        if error.reason is not None:
            # additive：仅治理拒绝携带结构化 reason，既有错误响应形态不变。
            body['reason'] = error.reason
        return JSONResponse(body, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse(error_body('invalid_request'), status_code=422)

    async def task_or_404(task_id):
        task = await asyncio.to_thread(app.state.repo.get, task_id)
        if task is None:
            raise APIError(404, 'task_not_found', task_id)
        return task

    @app.get('/', include_in_schema=False)
    async def home():
        return RedirectResponse('/console/' if console_dir.is_dir() else '/docs')

    @app.get('/api/v1/health')
    async def health():
        configured, model = False, None
        try:
            cfg = config_factory()
            configured, model = True, cfg.model
        except ValueError:
            pass
        await asyncio.to_thread(app.state.repo.list_tasks, 1, 1)
        return {'status': 'ok', 'database': 'ready', 'mode': 'single_task_local',
                'model': {'configured': configured, 'id': model, 'live_probe': 'not_performed'}}

    @app.post('/api/v1/tasks', status_code=202)
    async def create_task(spec: TaskInput):
        try:
            return await app.state.service.submit(spec.model_dump())
        except BusyError:
            raise APIError(409, 'task_already_running') from None
        except ModelProfileError as error:
            raise APIError(422, str(error)) from None
        except DataPolicyError as error:
            # 必须排在 TargetPolicyError/ValueError 之前：两者均为 ValueError 子类。
            raise APIError(422, error.code, reason=error.reason) from None
        except TargetPolicyError as error:
            # 必须排在 ValueError 之前：TargetPolicyError 是 ValueError 子类。
            raise APIError(422, error.code) from None
        except ValueError:
            raise APIError(503, 'model_not_configured') from None

    @app.post('/api/v1/import/curl')
    async def import_curl(body: CurlInput):
        try:
            policy = resolve_policy(body.policy_sha256, app.state.service.policies)
            return parse_curl(body.text, policy)
        except TargetPolicyError as error:
            raise APIError(422, error.code) from None
        except ValueError:
            raise APIError(422, 'unsupported_curl') from None

    @app.get('/api/v1/policies')
    async def list_policies():
        return {'items': [
            {'sha256': policy_sha256(policy), 'mode': policy.mode,
             'domains': [rule.domain for rule in policy.allowlist]}
            for policy in app.state.service.policies]}

    @app.get('/api/v1/models')
    async def list_models():
        return {'items': app.state.model_profiles.list()}

    @app.post('/api/v1/models', status_code=201)
    async def add_model(body: ProfileInput):
        try:
            return app.state.model_profiles.add(body)
        except ModelProfileError as error:
            raise APIError(422, str(error)) from None

    @app.post('/api/v1/models/{profile_id}/check')
    async def check_model(profile_id: str):
        try:
            return await app.state.model_profiles.probe(profile_id)
        except ModelProfileError as error:
            raise APIError(422, str(error)) from None
        except GatewayError as error:
            raise APIError(502, error.code) from None

    @app.delete('/api/v1/models/{profile_id}')
    async def remove_model(profile_id: str):
        try:
            app.state.model_profiles.remove(profile_id)
            return {'removed': True}
        except ModelProfileError as error:
            raise APIError(404, str(error)) from None

    @app.get('/api/v1/tasks')
    async def list_tasks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return await asyncio.to_thread(app.state.repo.list_tasks, page, page_size)

    @app.get('/api/v1/tasks/{task_id}')
    async def task_detail(task_id: str):
        return await task_or_404(task_id)

    @app.get('/api/v1/tasks/{task_id}/events')
    async def events(task_id: str, after_seq: int = Query(0, ge=0)):
        await task_or_404(task_id)
        return {'items': await asyncio.to_thread(app.state.repo.events, task_id, after_seq)}

    @app.post('/api/v1/tasks/{task_id}/cancel')
    async def cancel(task_id: str):
        result = await app.state.service.cancel(task_id)
        if result is None:
            raise APIError(404, 'task_not_found', task_id)
        return result

    @app.post('/api/v1/tasks/{task_id}/confirm')
    async def confirm(task_id: str, body: ConfirmationInput):
        await task_or_404(task_id)
        if not await app.state.service.confirm(task_id, body.plan_hash):
            raise APIError(409, 'preview_not_available_or_changed', task_id)
        return {'accepted': True, 'task_id': task_id}

    @app.get('/api/v1/tasks/{task_id}/artifacts')
    async def artifacts(task_id: str):
        task = await task_or_404(task_id)
        if task['status'] not in TERMINAL:
            return {'items': []}
        return {'items': await asyncio.to_thread(app.state.repo.artifacts, task_id)}

    async def artifact_content(entry):
        task = await task_or_404(entry['task_id'])
        if task['status'] not in TERMINAL:
            raise APIError(409, 'task_not_finished', entry['task_id'])
        try:
            return await asyncio.to_thread(app.state.repo.read_artifact, entry)
        except (ValueError, OSError):
            raise APIError(409, 'artifact_changed', entry['task_id']) from None

    @app.get('/api/v1/tasks/{task_id}/evidence')
    async def evidence(task_id: str):
        task = await task_or_404(task_id)
        if task['status'] not in TERMINAL:
            return []
        entries = await asyncio.to_thread(app.state.repo.artifacts, task_id)
        entry = next((a for a in entries if a['filename'] == 'evidence.json'), None)
        if entry is None:
            return []
        return json.loads(await artifact_content(entry))

    @app.get('/api/v1/tasks/{task_id}/artifacts/{filename}')
    async def artifact_document(task_id: str, filename: str):
        """M5.4-A：内联查看已注册的 JSON 契约产物（只读，复用 sha256 校验）。

        非 JSON 产物（collector.py / report.md）不在白名单内，保持 download-only；
        文件名必须命中白名单，因此不存在任意路径读取面。
        """
        if filename not in INLINE_ARTIFACTS:
            raise APIError(404, 'artifact_not_found', task_id)
        await task_or_404(task_id)
        entries = await asyncio.to_thread(app.state.repo.artifacts, task_id)
        entry = next((item for item in entries if item['filename'] == filename), None)
        if entry is None:
            raise APIError(404, 'artifact_not_found', task_id)
        raw = await artifact_content(entry)
        try:
            value = json.loads(raw)
        except ValueError:
            raise APIError(409, 'artifact_unreadable', task_id) from None
        return JSONResponse(value, headers={'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/v1/tasks/{task_id}/trace')
    async def trace(task_id: str):
        """M5.4-A：只读 Trace 投影。

        完全由 task_events 与已注册产物派生，不写任何文件、不新增产物、不调用模型；
        运行中的任务同样可读（此时产物尚未注册，步骤显示为 pending）。证据不可读时
        一律按缺失处理，投影不会猜测。
        """
        task = await task_or_404(task_id)
        events = await asyncio.to_thread(app.state.repo.events, task_id)
        entries = await asyncio.to_thread(app.state.repo.artifacts, task_id)
        artifacts = {}
        for entry in entries:
            if entry['filename'] not in INLINE_ARTIFACTS:
                continue
            try:
                artifacts[entry['filename']] = json.loads(
                    await asyncio.to_thread(app.state.repo.read_artifact, entry))
            except (ValueError, OSError):
                artifacts[entry['filename']] = None
        return project_trace(task, events, artifacts, entries)

    async def result_rows(task_id):
        await task_or_404(task_id)
        entries = await asyncio.to_thread(app.state.repo.artifacts, task_id)
        entry = next((item for item in entries if item['filename'] == 'result.json'), None)
        if entry is None:
            raise APIError(404, 'result_not_available', task_id)
        raw = await artifact_content(entry)
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError()
        except ValueError:
            raise APIError(409, 'artifact_unreadable', task_id) from None
        return rows, raw

    @app.get('/api/v1/tasks/{task_id}/result')
    async def result_page(task_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        rows, _ = await result_rows(task_id)
        task = await task_or_404(task_id)
        sources = (task.get('summary') or {}).get('source_urls', [])
        return {'items': rows[(page-1)*page_size:page*page_size], 'total': len(rows), 'page': page,
                'fields': list(rows[0]) if rows else [],
                'source_urls': sources[(page-1)*page_size:page*page_size] if isinstance(sources, list) else []}

    @app.get('/api/v1/tasks/{task_id}/export/{format}')
    async def export_result(task_id: str, format: Literal['json', 'csv', 'xlsx']):
        rows, raw = await result_rows(task_id)
        if format == 'csv':
            raw, media = await asyncio.to_thread(export_csv, rows), 'text/csv; charset=utf-8'
        elif format == 'xlsx':
            raw, media = await asyncio.to_thread(export_xlsx, rows), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        else:
            media = 'application/json'
        return Response(raw, media_type=media, headers={'Content-Disposition': f'attachment; filename="result.{format}"',
                                                      'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/v1/artifacts/{artifact_id}/download')
    async def download(artifact_id: str):
        entry = await asyncio.to_thread(app.state.repo.artifact, artifact_id)
        if entry is None:
            raise APIError(404, 'artifact_not_found')
        raw = await artifact_content(entry)
        if entry['filename'].endswith('.json'):
            media = 'application/json'
        elif entry['filename'].endswith('.py'):
            media = 'text/x-python'
        else:
            media = 'text/markdown'
        return Response(raw, media_type=media, headers={
            'Content-Disposition': f'attachment; filename="{entry["filename"]}"',
            'X-Content-Type-Options': 'nosniff',
        })

    console_dir = Path(console_directory) if console_directory is not None else Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
    if console_dir.is_dir():
        app.mount('/console', StaticFiles(directory=console_dir, html=True), name='console')
    return app


app = create_app()
