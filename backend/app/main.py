"""任务 API。启动：python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .llm.config import CloudConfig
from .pipeline.contracts import local_origin, has_sensitive_keys, SENSITIVE
from .pipeline.curl_import import parse_curl, checked_url
from .storage.instance_lock import InstanceLock
from .storage.repository import Repository, BusyError, TERMINAL
from .tasks.service import TaskService, pipeline_worker


class TaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    url: str = Field(default='http://127.0.0.1:8000/', max_length=512)
    fields: list[str] = Field(default_factory=lambda: ['id', 'name', 'price_fen'], min_length=1, max_length=10)
    max_pages: int = Field(default=3, ge=1, le=10)
    click_text: str = Field(default='下一页', max_length=80)
    rag_enabled: bool = True
    imported_url: str | None = Field(default=None, max_length=512)
    # 复用 curl_import 已解析的请求元数据；不再二次解析原始 cURL 文本。
    imported_method: str = Field(default='GET', max_length=8)
    imported_request_body: dict | None = Field(default=None)

    @field_validator('imported_url')
    @classmethod
    def check_imported(cls, value):
        return checked_url(value) if value is not None else None

    @model_validator(mode='after')
    def check_imported_request(self):
        """导入元数据必须与 imported_url 同时出现，且方法与请求体形态一致。"""
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

    @field_validator('url')
    @classmethod
    def check_url(cls, value):
        local_origin(value)
        if urlsplit(value).query:
            raise ValueError('entry_query_not_supported')
        return value

    @field_validator('fields')
    @classmethod
    def check_fields(cls, values):
        if len(set(values)) != len(values) or any(
            not re.fullmatch(r'\w{1,40}', value) or SENSITIVE.search(value) for value in values
        ):
            raise ValueError('invalid_fields')
        return values


class APIError(Exception):
    def __init__(self, status, code, task_id=None):
        self.status, self.code, self.task_id = status, code, task_id


class CurlInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(max_length=16384)


def error_body(code, task_id=None):
    return {'code': code, 'message': code, 'task_id': task_id, 'retryable': False}


def create_app(data_dir=None, config_factory=CloudConfig.deepseek_from_env, worker=pipeline_worker, console_directory=None):
    root = Path(data_dir).resolve() if data_dir is not None else Path(__file__).resolve().parents[2] / 'data'

    @asynccontextmanager
    async def lifespan(app):
        instance_lock = InstanceLock(root)
        repo = Repository(root)
        try:
            await asyncio.to_thread(repo.initialize)
            await asyncio.to_thread(repo.recover)
            service = TaskService(repo, config_factory, worker)
            app.state.repo, app.state.service = repo, service
            try:
                yield
            finally:
                await service.shutdown()
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
        return JSONResponse(error_body(error.code, error.task_id), status_code=error.status)

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
        except ValueError:
            raise APIError(503, 'model_not_configured') from None

    @app.post('/api/v1/import/curl')
    async def import_curl(body: CurlInput):
        try:
            return parse_curl(body.text)
        except ValueError:
            raise APIError(422, 'unsupported_curl') from None

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
