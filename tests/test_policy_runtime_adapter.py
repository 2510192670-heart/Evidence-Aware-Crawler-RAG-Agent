"""M7-A S3.1: TargetPolicy runtime adapter 测试。

验证策略传递链 request → spec → worker → run_task，三组覆盖：
A. TaskInput 无 policy → 全链路回落 loopback 默认策略（旧行为不变）；
B. TaskInput 携带服务端注册策略的 sha256 引用 → runtime 收到对应 TargetPolicy；
C. 非法/未注册引用 → TargetPolicyError（稳定码），API 层映射 422。

S3.1 边界：公网执行未开放——run_task 对非 loopback 策略以
``public_mode_not_enabled`` fail-closed，且不触达观察/执行；
policy 对象不可由请求体直接提交（extra='forbid'）。
完全离线：stub worker / stub observe，不发网络请求，不启动浏览器。
"""

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.llm.config import CloudConfig
from backend.app.main import TaskInput, create_app
from backend.app.pipeline import run as run_module
from backend.app.policy import (AllowRule, TargetPolicy, TargetPolicyError,
                                default_policy, policy_sha256)
from backend.app.policy import errors as policy_errors
from backend.app.pipeline.errors import Category, classify
from backend.app.tasks import service as service_module
from backend.app.tasks.service import pipeline_worker, resolve_policy

PUBLIC_POLICY = TargetPolicy(mode='public_http',
                             allowlist=[AllowRule(domain='example.com', include_subdomains=True)])
PUBLIC_SHA = policy_sha256(PUBLIC_POLICY)
SPEC = {'url': 'http://127.0.0.1:8000/', 'fields': ['id', 'name'], 'max_pages': 1,
        'click_text': '下一页'}


def config():
    return CloudConfig('https://example.test', 'not-a-real-secret', 'fake-model')


def make_spy_worker(captured):
    async def worker(spec, cfg, task_id, root, stage):
        captured.append(spec)
        folder = root / 'tasks' / task_id
        folder.mkdir(parents=True, exist_ok=True)
        report = {'status': 'succeeded', 'count': 0, 'model_calls': 0, 'completeness': 'complete'}
        (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        return report
    return worker


def wait_terminal(client, task_id):
    for _ in range(200):
        task = client.get(f'/api/v1/tasks/{task_id}').json()
        if task['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
            return task
        time.sleep(0.01)
    pytest.fail('Task did not finish')


def run_task_report(tmp_path, monkeypatch, *, policy, observe_calls):
    """在 stub 观察下跑 run_task，返回 report.json 内容。"""
    async def fake_observe(url, click_text=None):
        observe_calls.append(url)
        raise ValueError('no_usable_json_requests')

    async def fake_observe_imported(url, method='GET', request_body=None):
        observe_calls.append(url)
        raise ValueError('no_usable_json_requests')

    monkeypatch.setattr(run_module, 'observe', fake_observe)
    monkeypatch.setattr(run_module, 'observe_imported', fake_observe_imported)
    task_id = str(uuid.uuid4())
    args = SimpleNamespace(url='http://127.0.0.1:8000/', fields='id', max_pages=1,
                           click_text='', rag_enabled=False, imported_url=None)
    exit_code = asyncio.run(run_module.run_task(args, config(), task_id=task_id,
                                                output_root=tmp_path, quiet=True, policy=policy))
    report = json.loads((tmp_path / 'tasks' / task_id / 'report.json').read_text(encoding='utf-8'))
    assert exit_code == 1
    return report


# --- A. 无 policy → loopback -------------------------------------------------

def test_taskinput_policy_reference_defaults_to_none():
    assert TaskInput().policy_sha256 is None


def test_resolve_policy_none_returns_loopback_default():
    policy = resolve_policy(None)
    assert policy.mode == 'loopback'
    assert policy_sha256(policy) == policy_sha256(default_policy())
    # 注册表内容与 None 解析无关：空表/非空表结果一致。
    assert resolve_policy(None, [PUBLIC_POLICY]) == policy


def test_task_without_policy_gets_loopback_policy_in_spec(tmp_path):
    captured = []
    with TestClient(create_app(tmp_path, config, make_spy_worker(captured))) as client:
        response = client.post('/api/v1/tasks', json=SPEC)
        assert response.status_code == 202
        task = wait_terminal(client, response.json()['id'])
        assert task['status'] == 'succeeded'
        # 服务端把规范化 loopback 策略注入 spec 并随任务持久化（审计链）。
        assert task['spec']['policy'] == default_policy().model_dump(mode='json')
        assert task['spec']['policy']['mode'] == 'loopback'
    assert captured[0]['policy_sha256'] is None
    assert captured[0]['policy']['mode'] == 'loopback'


def test_run_task_without_policy_keeps_old_loopback_path(tmp_path, monkeypatch):
    calls = []
    report = run_task_report(tmp_path, monkeypatch, policy=None, observe_calls=calls)
    # 守卫放行，进入原有观察路径。observe 的 no_usable_json_requests 是未迁移的
    # 裸 ValueError，classify 按现状 fail-closed 为 UNCLASSIFIED（error='ValueError'）——
    # 该行为在 S3.1 前后必须一致，正是"旧逻辑不变"的证据。
    assert report['status'] == 'failed'
    assert report['error'] == 'ValueError'
    assert report['error_category'] == 'UNCLASSIFIED'
    assert calls == ['http://127.0.0.1:8000/']


# --- B. sha256 引用 → runtime 收到对应策略 ------------------------------------

def test_resolve_policy_returns_registered_policy():
    assert resolve_policy(PUBLIC_SHA, [PUBLIC_POLICY]) == PUBLIC_POLICY


def test_referenced_policy_reaches_runtime_spec(tmp_path):
    # S3.2-B1 起 admission 要求入口 URL 命中策略：公网策略须配 allowlist 内的 URL
    # （loopback URL + 公网 policy 的"不匹配拒绝"用例见 test_policy_task_admission.py）。
    captured = []
    with TestClient(create_app(tmp_path, config, make_spy_worker(captured),
                               policies=[PUBLIC_POLICY])) as client:
        response = client.post('/api/v1/tasks',
                               json={**SPEC, 'url': 'https://example.com/',
                                     'policy_sha256': PUBLIC_SHA})
        assert response.status_code == 202
        task = wait_terminal(client, response.json()['id'])
        assert task['spec']['policy'] == PUBLIC_POLICY.model_dump(mode='json')
    assert captured[0]['policy']['mode'] == 'public_http'
    assert TargetPolicy(**captured[0]['policy']) == PUBLIC_POLICY


def test_pipeline_worker_rebuilds_args_policy(tmp_path, monkeypatch):
    seen = {}

    async def fake_run_task(args, cfg, *, task_id, output_root, on_stage, quiet):
        seen['policy'] = args.policy
        folder = output_root / 'tasks' / task_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'report.json').write_text(json.dumps({'status': 'succeeded'}), encoding='utf-8')

    monkeypatch.setattr(service_module, 'run_task', fake_run_task)
    spec = {**SPEC, 'fields': ['id'], 'policy': PUBLIC_POLICY.model_dump(mode='json')}

    async def stage(name):
        pass

    asyncio.run(pipeline_worker(spec, config(), str(uuid.uuid4()), tmp_path, stage))
    assert seen['policy'] == PUBLIC_POLICY


def test_run_task_rejects_public_policy_before_any_io(tmp_path, monkeypatch):
    calls = []
    report = run_task_report(tmp_path, monkeypatch, policy=PUBLIC_POLICY, observe_calls=calls)
    # S3.1：公网策略在观察之前 fail-closed，绝不触达观察/执行。
    assert report['status'] == 'failed'
    assert report['error'] == 'public_mode_not_enabled'
    assert report['error_type'] == 'TargetPolicyError'
    assert report['error_repairable'] is False and report['error_retryable'] is False
    assert calls == []


def test_run_task_accepts_policy_via_args(tmp_path, monkeypatch):
    """service 注入路径：args.policy（非显式 kwarg）同样生效。"""
    calls = []

    async def fake_observe(url, click_text=None):
        calls.append(url)
        raise ValueError('no_usable_json_requests')

    monkeypatch.setattr(run_module, 'observe', fake_observe)
    task_id = str(uuid.uuid4())
    args = SimpleNamespace(url='http://127.0.0.1:8000/', fields='id', max_pages=1,
                           click_text='', rag_enabled=False, imported_url=None,
                           policy=PUBLIC_POLICY)
    assert asyncio.run(run_module.run_task(args, config(), task_id=task_id,
                                           output_root=tmp_path, quiet=True)) == 1
    report = json.loads((tmp_path / 'tasks' / task_id / 'report.json').read_text(encoding='utf-8'))
    assert report['error'] == 'public_mode_not_enabled'
    assert calls == []


# --- C. 非法引用 → TargetPolicyError ------------------------------------------

@pytest.mark.parametrize('reference', ['xyz', '', 'F' * 64, 'f' * 63, 123,
                                       'https://example.com/policy.json'])
def test_resolve_policy_rejects_malformed_reference(reference):
    with pytest.raises(TargetPolicyError) as excinfo:
        resolve_policy(reference, [PUBLIC_POLICY])
    assert excinfo.value.code == 'invalid_policy_reference'
    assert str(excinfo.value) == 'invalid_policy_reference'


def test_resolve_policy_rejects_unknown_reference():
    with pytest.raises(TargetPolicyError) as excinfo:
        resolve_policy('f' * 64, [PUBLIC_POLICY])
    assert excinfo.value.code == 'policy_not_found'
    with pytest.raises(TargetPolicyError):
        resolve_policy(PUBLIC_SHA, [])      # 未部署该策略的注册表


def test_api_maps_policy_errors_to_422(tmp_path):
    captured = []
    with TestClient(create_app(tmp_path, config, make_spy_worker(captured),
                               policies=[PUBLIC_POLICY])) as client:
        unknown = client.post('/api/v1/tasks', json={**SPEC, 'policy_sha256': 'f' * 64})
        assert unknown.status_code == 422
        assert unknown.json()['code'] == 'policy_not_found'
        malformed = client.post('/api/v1/tasks', json={**SPEC, 'policy_sha256': 'xyz'})
        assert malformed.status_code == 422
        assert malformed.json()['code'] == 'invalid_policy_reference'
    assert captured == []                   # 被拒引用绝不触达 worker


def test_api_rejects_inline_policy_object(tmp_path):
    """用户不能直接提交 policy 对象：extra='forbid' → 422 invalid_request。"""
    with TestClient(create_app(tmp_path, config, make_spy_worker([]),
                               policies=[PUBLIC_POLICY])) as client:
        response = client.post('/api/v1/tasks',
                               json={**SPEC, 'policy': PUBLIC_POLICY.model_dump(mode='json')})
        assert response.status_code == 422
        assert response.json()['code'] == 'invalid_request'


def test_adapter_codes_fail_closed_until_registered():
    """S3.1 新码未入 SPECS：classify 必须 UNCLASSIFIED、不可 repair/retry。"""
    for code in ('invalid_policy_reference', 'policy_not_found', 'public_mode_not_enabled'):
        assert code not in policy_errors.__dict__      # 非本模块常量，仅字符串约定
        classification = classify(TargetPolicyError(code))
        assert classification.category is Category.UNCLASSIFIED, code
        assert classification.repairable is False and classification.retryable is False, code
