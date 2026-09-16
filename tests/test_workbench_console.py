"""Actual Vue + FastAPI/storage bridge; fixture worker makes no model requests."""
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from backend.app.main import create_app
from backend.app.policy import AllowRule, TargetPolicy


async def preview_worker(spec, config, task_id, root, stage):
    await stage('analyzing')
    await stage.preview({'plan_hash': 'a' * 64, 'items': [{'id': 1, 'name': '真实测试数据'}], 'count': 1})
    return {'status': 'succeeded', 'count': 1, 'pages': 1, 'model_calls': 0}


def test_model_policy_requirements_and_confirmation_ui(tmp_path):
    def missing_config():
        raise ValueError('not_configured')

    policy = TargetPolicy(mode='public_http', allowlist=(AllowRule(domain='example.com'),))
    app = create_app(tmp_path, missing_config, preview_worker, policies=[policy])
    with TestClient(app) as client, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def bridge(route):
            req = route.request
            path = urlsplit(req.url).path
            if urlsplit(req.url).query:
                path += '?' + urlsplit(req.url).query
            response = client.request(req.method, path, content=req.post_data,
                                      headers={'content-type': 'application/json'} if req.post_data else {})
            route.fulfill(status=response.status_code, body=response.content,
                          headers=dict(response.headers))

        page.route('**/*', bridge)
        try:
            page.goto('http://127.0.0.1/console/')
            expect(page.get_by_role('heading', name='任务控制台')).to_be_visible()
            page.get_by_text('配置自己的模型', exact=True).click()
            page.get_by_label('模型名称', exact=True).fill('fixture-model')
            page.get_by_label('API 密钥', exact=True).fill('fixture-private-key')
            page.get_by_role('button', name='保存并选择模型').click()
            expect(page.get_by_label('API 密钥', exact=True)).to_have_value('')
            page.get_by_label('采集范围', exact=True).select_option(label='已配置网站：example.com')
            page.get_by_label('页面地址', exact=True).fill('https://example.com/')
            page.get_by_label('采集需求', exact=True).fill('提取编号和名称')
            page.get_by_label('提取字段', exact=True).fill('id,name')
            page.get_by_role('button', name='创建任务', exact=True).click()
            expect(page.get_by_role('heading', name='确认采集样本')).to_be_visible(timeout=10000)
            expect(page.get_by_text('真实测试数据', exact=True)).to_be_visible()
            page.get_by_role('button', name='确认样本并开始采集').click()
            expect(page.locator('.detail-heading .status')).to_have_text('已完成', timeout=10000)
            row = client.get('/api/v1/tasks').json()['items'][0]
            assert row['spec']['description'] == '提取编号和名称'
            assert row['spec']['policy']['mode'] == 'public_http'
            assert row['model'] == 'fixture-model'
            assert 'fixture-private-key' not in str(row)
            assert errors == []
            page.screenshot(path=str(tmp_path / 'workbench-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(page.get_by_role('heading', name='任务控制台')).to_be_visible()
            page.screenshot(path=str(tmp_path / 'workbench-mobile.png'), full_page=True)
        finally:
            browser.close()
