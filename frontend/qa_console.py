"""Browser checks against the running console; default uses mocked API (no cloud calls).

Run from repository root: .venv/Scripts/python.exe frontend/qa_console.py
Pass --live to create ONE real task and verify its download.
"""
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright, expect


async def main(live=False):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 1505, 'height': 1045})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        submitted = []
        tasks = []
        offline = False
        async def api(route):
            if offline:
                return await route.abort()
            path = route.request.url.split('/api/v1')[1].split('?')[0]
            if path == '/health':
                value = {'model': {'configured': True}}
            elif path == '/tasks' and route.request.method == 'POST':
                spec = route.request.post_data_json
                submitted.append(spec)
                value = {'id': 'test-task', 'status': 'observing', 'created_at': '2026-09-13T09:00:00+08:00', 'spec': spec, 'summary': None}
                tasks[:] = [value]
            elif path == '/tasks':
                value = {'items': tasks, 'total': len(tasks)}
            elif path.endswith('/cancel'):
                tasks[0]['status'] = 'cancelled'
                tasks[0]['summary'] = {'error': 'cancelled', 'model_calls': 0}
                value = tasks[0]
            elif path.endswith('/events'):
                value = {'items': [{'seq': 1, 'status': 'created'}, {'seq': 2, 'status': 'observing'}]}
            elif path.endswith('/artifacts'):
                value = {'items': []}
            else:
                value = tasks[0]
            await route.fulfill(json=value)
        if not live:
            await page.route('**/api/v1/**', api)
        await page.goto('http://127.0.0.1:8002/console/')
        await expect(page.get_by_role('button', name='创建任务', exact=True)).to_be_enabled()
        if not live:
            await expect(page.get_by_text('还没有任务，从左侧创建第一个任务。')).to_be_visible()
            await page.get_by_label('使用案例 RAG').uncheck()
        await page.get_by_role('button', name='创建任务', exact=True).click()
        if live:
            await expect(page.get_by_text('完整性验证通过', exact=False)).to_be_visible(timeout=150000)
            task_id = page.url.split('#/')[1]
            async with page.expect_download() as info:
                await page.get_by_role('link', name='下载 result.json', exact=True).click()
            download = await info.value
            rows = json.loads(Path(await download.path()).read_text(encoding='utf-8'))
            assert len(rows) == 30
            assert [row['id'] for row in rows] == list(range(1,31))
            await page.reload()
            await expect(page.get_by_text('完整性验证通过', exact=False)).to_be_visible()
            assert page.url.endswith(task_id)
            print('LIVE: task', task_id, '30 rows downloaded; selection survives reload')
        else:
            await expect(page.get_by_role('button', name='取消任务')).to_be_visible()
            assert len(submitted) == 1 and submitted[0]['rag_enabled'] is False
            await expect(page.get_by_role('button', name='创建任务', exact=True)).to_be_disabled()
            await page.get_by_role('button', name='取消任务').click()
            await expect(page.get_by_role('button', name='创建任务', exact=True)).to_be_enabled()
            await expect(page.get_by_text('任务已取消：cancelled')).to_be_visible()
            offline = True
            await expect(page.get_by_role('alert')).to_contain_text('无法连接服务', timeout=10000)
            offline = False
            await page.get_by_role('button', name='重试').click()
            await expect(page.get_by_role('alert')).to_have_count(0)
            print('MOCK: empty/create/RAG/busy/cancel/offline/retry passed; zero cloud calls')
        await page.set_viewport_size({'width': 1280, 'height': 800})
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert not errors, errors
        await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    asyncio.run(main(parser.parse_args().live))
