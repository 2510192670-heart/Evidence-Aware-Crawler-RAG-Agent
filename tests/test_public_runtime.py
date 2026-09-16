"""M7-A S3.3-C3-B: run_task 公网编排测试（guard inversion / admission 序列 /
transport 选择 / robots 失败）。

完全离线：CloudGateway、observe、export_collector、build_public_transport 全部
stub/monkeypatch；httpx.AsyncClient 经工厂注入 MockTransport——不访问真实公网、
不调用云模型、不启动浏览器。loopback 等价性以 policy=None 与显式 loopback
双跑比对证明。
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from backend.app.pipeline import run as run_module
from backend.app.pipeline.contracts import ExtractionPlan, Observation
from backend.app.policy import AllowRule, TargetPolicy, default_policy

PUBLIC_POLICY = TargetPolicy(mode='public_http',
                             allowlist=[AllowRule(domain='example.com', include_subdomains=True)])
PUBLIC_RESOLVER = lambda host: ['93.184.216.34']          # noqa: E731（离线假解析）
PRIVATE_RESOLVER = lambda host: ['127.0.0.1']             # noqa: E731

PAGE_1 = {'items': [{'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}], 'total': 4}
PAGE_2 = {'items': [{'id': 3, 'name': 'c'}, {'id': 4, 'name': 'd'}], 'total': 4}
PLAN = ExtractionPlan(request_id='req_001', items_pointer='/items',
                      fields={'id': '/id', 'name': '/name'}, unique_key='id',
                      has_next_pointer=None, page_parameter='page', total_pointer='/total')

PUBLIC_OBSERVATION = Observation(request_id='req_001', url='https://example.com/api',
                                 query={'page': '1'}, body=dict(PAGE_1))
LOOPBACK_OBSERVATION = Observation(request_id='req_001', url='http://127.0.0.1:8000/api',
                                   query={'page': '1'}, body=dict(PAGE_1))


class StubGateway:
    """替代 CloudGateway：固定返回 PLAN，绝不出网。"""

    def __init__(self, config):
        self.config = config
        self.calls = 0
        self.usage_history = []

    async def generate(self, payload, schema):
        self.calls += 1
        return SimpleNamespace(data=PLAN, usage=None, model='stub', elapsed_seconds=0.0)


def make_handler(log, robots_status=404, robots_body=''):
    def handler(request):
        log.append(request)
        if request.url.path == '/robots.txt':
            return httpx.Response(robots_status, text=robots_body)
        page = int(request.url.params.get('page', '1'))
        return httpx.Response(200, json=PAGE_1 if page == 1 else PAGE_2)
    return handler


def install_stubs(monkeypatch, handler, observations, seq=None, build_calls=None):
    """统一的离线桩：gateway/observe/export/transport/client 工厂。"""
    observe_calls = []

    async def fake_observe(url, click_text=None, *, policy=None, resolver=None):
        observe_calls.append({'url': url, 'policy': policy})
        if seq is not None:
            seq.append('observe')
        return list(observations)

    async def fake_export(folder, plan, record, collected, max_pages):
        return {'collector_exported': False, 'collector_execution_success': False,
                'collector_matches_internal_result': False}

    def fake_build(resolver=None, per_domain_rps=2.0):
        if build_calls is not None:
            build_calls.append(resolver)
        if seq is not None:
            seq.append('transport')
        return httpx.MockTransport(handler)

    client_kwargs = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client_kwargs.append(dict(kwargs))              # 记录副本：下方会就地注入 mock
        if kwargs.get('transport') is None:
            kwargs['transport'] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(run_module, 'CloudGateway', StubGateway)
    monkeypatch.setattr(run_module, 'observe', fake_observe)
    monkeypatch.setattr(run_module, 'export_collector', fake_export)
    monkeypatch.setattr(run_module, 'build_public_transport', fake_build)
    monkeypatch.setattr(httpx, 'AsyncClient', client_factory)
    return {'observe_calls': observe_calls, 'client_kwargs': client_kwargs}


def public_args(**overrides):
    base = dict(url='https://example.com/list', fields='id,name', max_pages=2,
                click_text='', rag_enabled=False, imported_url=None, resolver=PUBLIC_RESOLVER)
    base.update(overrides)
    return SimpleNamespace(**base)


def drive(tmp_path, args, policy):
    task_id = str(uuid.uuid4())
    exit_code = asyncio.run(run_module.run_task(args, SimpleNamespace(model='stub-model'),
                                                task_id=task_id, output_root=tmp_path,
                                                quiet=True, policy=policy))
    report = json.loads((tmp_path / 'tasks' / task_id / 'report.json').read_text(encoding='utf-8'))
    return exit_code, report


# --- guard inversion ----------------------------------------------------------

def test_guard_inverted_public_task_succeeds(tmp_path, monkeypatch):
    log = []
    install_stubs(monkeypatch, make_handler(log), [PUBLIC_OBSERVATION])
    exit_code, report = drive(tmp_path, public_args(), PUBLIC_POLICY)
    assert exit_code == 0
    assert report['status'] == 'succeeded'
    assert 'public_mode_not_enabled' not in json.dumps(report)
    assert report['count'] == 4 and report['pages'] == 2
    assert report['completeness'] == 'complete'
    assert report['source'] == 'browser'
    paths = [r.url.path for r in log]
    assert paths[0] == '/robots.txt'
    assert paths.count('/api') == 2                      # 两页数据请求


# --- admission sequence --------------------------------------------------------

def test_admission_sequence_is_ordered(tmp_path, monkeypatch):
    seq = []
    log = []
    install_stubs(monkeypatch, make_handler(log), [PUBLIC_OBSERVATION], seq=seq)

    original_check = run_module.policy_check_target
    original_resolve = run_module.resolve_and_classify
    original_robots = run_module.fetch_robots

    def spy_check(url, policy=None):
        if 'check_target' not in seq:
            seq.append('check_target')
        return original_check(url, policy)

    def spy_resolve(hostname, resolver=None):
        seq.append('resolve')
        return original_resolve(hostname, resolver)

    async def spy_robots(origin, transport):
        seq.append('robots')
        return await original_robots(origin, transport)

    def logging_handler(request):
        if request.url.path != '/robots.txt' and 'execute' not in seq:
            seq.append('execute')
        return make_handler(log)(request)

    monkeypatch.setattr(run_module, 'policy_check_target', spy_check)
    monkeypatch.setattr(run_module, 'resolve_and_classify', spy_resolve)
    monkeypatch.setattr(run_module, 'fetch_robots', spy_robots)
    monkeypatch.setattr(run_module, 'build_public_transport',
                        lambda resolver=None, per_domain_rps=2.0: (seq.append('transport'),
                                                                   httpx.MockTransport(logging_handler))[1])
    exit_code, report = drive(tmp_path, public_args(), PUBLIC_POLICY)
    assert report['status'] == 'succeeded'
    assert seq == ['check_target', 'resolve', 'transport', 'robots', 'observe', 'execute']


def test_ssrf_admission_blocks_before_transport_and_observe(tmp_path, monkeypatch):
    seq = []
    build_calls = []
    log = []
    stubs = install_stubs(monkeypatch, make_handler(log), [PUBLIC_OBSERVATION],
                          seq=seq, build_calls=build_calls)
    exit_code, report = drive(tmp_path, public_args(resolver=PRIVATE_RESOLVER), PUBLIC_POLICY)
    assert exit_code == 1
    assert report['status'] == 'failed'
    assert report['error'] == 'ssrf_ip_blocked'
    assert report['error_category'] == 'SECURITY'
    assert build_calls == [] and 'observe' not in seq
    assert stubs['observe_calls'] == []
    assert log == []                                      # 零网络副作用


# --- transport selection ---------------------------------------------------------

def test_transport_selection_public_vs_loopback(tmp_path, monkeypatch):
    # public：build_public_transport 被调用，execute client 收到受守卫 transport。
    log, build_calls = [], []
    stubs = install_stubs(monkeypatch, make_handler(log), [PUBLIC_OBSERVATION],
                          build_calls=build_calls)
    drive(tmp_path, public_args(), PUBLIC_POLICY)
    assert len(build_calls) == 1
    assert any(kwargs.get('transport') is not None for kwargs in stubs['client_kwargs'])

    # loopback：不构造公网 transport，client 构造参数 transport=None（与旧构造等价）。
    log2, build_calls2 = [], []
    stubs2 = install_stubs(monkeypatch, make_handler(log2), [LOOPBACK_OBSERVATION],
                           build_calls=build_calls2)
    args = SimpleNamespace(url='http://127.0.0.1:8000/list', fields='id,name', max_pages=2,
                           click_text='', rag_enabled=False, imported_url=None)
    drive(tmp_path, args, None)
    assert build_calls2 == []
    assert all(kwargs.get('transport') is None for kwargs in stubs2['client_kwargs'])


# --- robots ------------------------------------------------------------------------

def test_robots_entry_disallow_blocks_before_observe(tmp_path, monkeypatch):
    log = []
    stubs = install_stubs(monkeypatch,
                          make_handler(log, robots_status=200, robots_body='User-agent: *\nDisallow: /'),
                          [PUBLIC_OBSERVATION])
    exit_code, report = drive(tmp_path, public_args(), PUBLIC_POLICY)
    assert report['status'] == 'failed'
    assert report['error'] == 'robots_disallowed'
    assert report['error_category'] == 'SECURITY'
    assert stubs['observe_calls'] == []
    assert [r.url.path for r in log] == ['/robots.txt']   # 只有 robots 抓取，无数据请求


def test_robots_execution_path_disallow_blocks_before_pages(tmp_path, monkeypatch):
    log = []
    install_stubs(monkeypatch,
                  make_handler(log, robots_status=200, robots_body='User-agent: *\nDisallow: /api'),
                  [PUBLIC_OBSERVATION])
    exit_code, report = drive(tmp_path, public_args(), PUBLIC_POLICY)
    assert report['status'] == 'failed'
    assert report['error'] == 'robots_disallowed'
    assert [r.url.path for r in log] == ['/robots.txt']   # 入口 /list 放行，执行 /api 被拒


def test_robots_fetch_failure_fail_closed(tmp_path, monkeypatch):
    log = []
    stubs = install_stubs(monkeypatch, make_handler(log, robots_status=500), [PUBLIC_OBSERVATION])
    exit_code, report = drive(tmp_path, public_args(), PUBLIC_POLICY)
    assert report['status'] == 'failed'
    assert report['error'] == 'robots_disallowed'
    assert stubs['observe_calls'] == []


# --- loopback 等价 -------------------------------------------------------------------

def test_loopback_none_and_default_policy_equivalent(tmp_path, monkeypatch):
    results = []
    for policy in (None, default_policy()):
        log = []
        stubs = install_stubs(monkeypatch, make_handler(log), [LOOPBACK_OBSERVATION])
        args = SimpleNamespace(url='http://127.0.0.1:8000/list', fields='id,name', max_pages=2,
                               click_text='', rag_enabled=False, imported_url=None)
        exit_code, report = drive(tmp_path, args, policy)
        assert exit_code == 0 and report['status'] == 'succeeded'
        # 等价性原则：loopback 分支保持旧调用形态，不向 observe 传新 kwarg。
        received = stubs['observe_calls'][0]['policy']
        assert received is None or received.mode == 'loopback'
        results.append({key: report[key] for key in
                        ('status', 'count', 'pages', 'completeness', 'expected_total', 'source')})
    assert results[0] == results[1]


# --- curl import 公网 e2e（离线） --------------------------------------------------------

def test_public_curl_import_branch_end_to_end(tmp_path, monkeypatch):
    log = []
    stubs = install_stubs(monkeypatch, make_handler(log), [])   # observe stub 不应被调用
    args = public_args(url='https://example.com/unused',
                       imported_url='https://example.com/api?page=1',
                       imported_method='GET', imported_request_body=None)
    exit_code, report = drive(tmp_path, args, PUBLIC_POLICY)
    assert exit_code == 0 and report['status'] == 'succeeded'
    assert report['source'] == 'curl_import'
    assert stubs['observe_calls'] == []
    assert report['count'] == 4 and report['pages'] == 2


def test_public_browser_entry_query_rejected(tmp_path, monkeypatch):
    """浏览器入口保留 entry-query 限制（运行时防线，API 层之外再拦一次）。"""
    log = []
    stubs = install_stubs(monkeypatch, make_handler(log), [PUBLIC_OBSERVATION])
    exit_code, report = drive(tmp_path, public_args(url='https://example.com/list?page=1'),
                              PUBLIC_POLICY)
    assert report['status'] == 'failed'
    assert report['error'] == 'entry_url_query_not_supported'
    assert stubs['observe_calls'] == []
