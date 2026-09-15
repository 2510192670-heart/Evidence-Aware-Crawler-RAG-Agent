"""M7-A S3.2-B1: 任务创建入口的 TargetPolicy admission 测试。

验证 POST /api/v1/tasks 的准入链：
    resolve_policy() → enforcement.check_target(url, policy)

五组覆盖：
A. 默认（无 policy_sha256）→ loopback 任务创建成功；
B. public_http + github.com 策略 + allowlist 内 HTTPS URL → 创建成功；
C. 公网 URL 未提供策略 → 拒绝（与 M7 之前的线上行为一致：422 invalid_request）；
D. allowlist 外域 / 策略不匹配（loopback URL + 公网策略）/ 非 https → 拒绝且带稳定码；
E. localhost → 维持 S2 快照基线：API 层 422 invalid_request，enforcement 层裸
   ValueError('only_literal_loopback_http_supported')。

admission 判定全部来自 backend.app.policy.enforcement.check_target（单一真源），
本测试不镜像任何规则。完全离线：stub worker，不发网络请求。
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.llm.config import CloudConfig
from backend.app.main import create_app
from backend.app.policy import AllowRule, TargetPolicy, TargetPolicyError, policy_sha256
from backend.app.policy.enforcement import check_target

GITHUB_POLICY = TargetPolicy(mode='public_http',
                             allowlist=[AllowRule(domain='github.com', include_subdomains=True)])
GITHUB_SHA = policy_sha256(GITHUB_POLICY)
BASE = {'fields': ['id', 'name'], 'max_pages': 1, 'click_text': '下一页'}


def config():
    return CloudConfig('https://example.test', 'not-a-real-secret', 'fake-model')


async def stub_worker(spec, cfg, task_id, root, stage):
    folder = root / 'tasks' / task_id
    folder.mkdir(parents=True, exist_ok=True)
    report = {'status': 'succeeded', 'count': 0, 'model_calls': 0, 'completeness': 'complete'}
    (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    return report


def wait_terminal(client, task_id):
    for _ in range(200):
        task = client.get(f'/api/v1/tasks/{task_id}').json()
        if task['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
            return task
        time.sleep(0.01)
    pytest.fail('Task did not finish')


# --- A. 默认 loopback 创建成功 ---------------------------------------------------

def test_default_loopback_task_admitted(tmp_path):
    with TestClient(create_app(tmp_path, config, stub_worker,
                               policies=[GITHUB_POLICY])) as client:
        response = client.post('/api/v1/tasks',
                               json={**BASE, 'url': 'http://127.0.0.1:8000/'})
        assert response.status_code == 202
        task = wait_terminal(client, response.json()['id'])
        assert task['status'] == 'succeeded'
        assert task['spec']['policy']['mode'] == 'loopback'


# --- B. public_http + github.com 策略创建成功 -------------------------------------

@pytest.mark.parametrize('url', ['https://github.com/', 'https://github.com/topics/json',
                                 'https://api.github.com/repos'])
def test_public_task_with_matching_policy_admitted(tmp_path, url):
    with TestClient(create_app(tmp_path, config, stub_worker,
                               policies=[GITHUB_POLICY])) as client:
        response = client.post('/api/v1/tasks',
                               json={**BASE, 'url': url, 'policy_sha256': GITHUB_SHA})
        assert response.status_code == 202, response.json()
        task = wait_terminal(client, response.json()['id'])
        # 创建级 admission 通过；策略与引用随 spec 持久化（审计链）。
        assert task['spec']['policy_sha256'] == GITHUB_SHA
        assert task['spec']['policy'] == GITHUB_POLICY.model_dump(mode='json')


# --- C. 公网 URL 未提供策略 → 拒绝 -------------------------------------------------

def test_public_url_without_policy_rejected(tmp_path):
    with TestClient(create_app(tmp_path, config, stub_worker,
                               policies=[GITHUB_POLICY])) as client:
        response = client.post('/api/v1/tasks', json={**BASE, 'url': 'https://github.com/'})
        # 与 M7 之前完全一致的线上行为：模型级 loopback 判定 → 422 invalid_request。
        assert response.status_code == 422
        assert response.json()['code'] == 'invalid_request'


# --- D. allowlist / 策略不匹配 / 非法形态 → 拒绝（稳定码） ---------------------------

@pytest.mark.parametrize('url,code', [
    ('https://evil.com/', 'target_not_in_allowlist'),            # allowlist 外域
    ('https://github.com.evil.net/', 'target_not_in_allowlist'),  # 点边界绕过
    ('https://notgithub.com/', 'target_not_in_allowlist'),
    # 策略不匹配（loopback URL + 公网策略）；拒绝码遵循单一真源的判定顺序
    # scheme → 端口 → query → 主机形态/allowlist。
    ('http://127.0.0.1:8000/', 'public_scheme_not_allowed'),
    ('https://127.0.0.1:8000/', 'unsupported_port'),
    ('https://127.0.0.1/', 'target_not_in_allowlist'),
    ('http://github.com/', 'public_scheme_not_allowed'),          # 非 https
    ('https://github.com:8443/', 'unsupported_port'),
    ('https://10.0.0.1/', 'target_not_in_allowlist'),             # 内网 IP
])
def test_public_admission_rejections_carry_stable_codes(tmp_path, url, code):
    with TestClient(create_app(tmp_path, config, stub_worker,
                               policies=[GITHUB_POLICY])) as client:
        response = client.post('/api/v1/tasks',
                               json={**BASE, 'url': url, 'policy_sha256': GITHUB_SHA})
        assert response.status_code == 422
        assert response.json()['code'] == code
        # 被拒任务不落库、不触达 worker。
        assert client.get('/api/v1/tasks').json()['total'] == 0


def test_unregistered_policy_reference_still_rejected_before_admission(tmp_path):
    """resolve_policy 在 admission 之前：未注册引用 → policy_not_found（S3.1 契约）。"""
    with TestClient(create_app(tmp_path, config, stub_worker,
                               policies=[GITHUB_POLICY])) as client:
        response = client.post('/api/v1/tasks',
                               json={**BASE, 'url': 'https://github.com/',
                                     'policy_sha256': 'f' * 64})
        assert response.status_code == 422
        assert response.json()['code'] == 'policy_not_found'


# --- E. localhost 行为与 S2 快照一致 ------------------------------------------------

def test_localhost_rejected_at_api_and_enforcement_layers(tmp_path):
    # API 层：无策略 → 模型级 loopback 判定 → 422 invalid_request（与 M7 之前一致）。
    with TestClient(create_app(tmp_path, config, stub_worker)) as client:
        response = client.post('/api/v1/tasks', json={**BASE, 'url': 'http://localhost:8000/'})
        assert response.status_code == 422
        assert response.json()['code'] == 'invalid_request'
    # enforcement 层：维持 S2 快照钉住的裸 ValueError 与旧码（不升级、不放宽）。
    with pytest.raises(ValueError) as excinfo:
        check_target('http://localhost:8000/')
    assert type(excinfo.value) is ValueError
    assert not isinstance(excinfo.value, TargetPolicyError)
    assert str(excinfo.value) == 'only_literal_loopback_http_supported'
