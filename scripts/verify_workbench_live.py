"""Explicit live acceptance: creates ONE public task and calls the configured model.

Run with --live after starting the workbench with the example public policy.
"""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from playwright.async_api import async_playwright, expect


async def verify(base):
    output = Path(tempfile.mkdtemp(prefix='wda-live-'))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        task_id = None
        try:
            await page.goto(base + '/console/')
            await expect(page.get_by_role('heading', name='任务控制台')).to_be_visible()
            await page.get_by_label('采集范围', exact=True).select_option(label='已配置网站：books.toscrape.com')
            await page.get_by_label('页面地址', exact=True).fill('https://books.toscrape.com/')
            await page.get_by_label('采集需求', exact=True).fill('提取图书完整名称、页面展示的原始价格文本和图书详情链接。')
            await page.get_by_label('提取字段', exact=True).fill('title,price,link')
            await page.get_by_label('提取方式', exact=True).select_option('html')
            await page.get_by_label('最多采集条数', exact=True).fill('3')
            await page.get_by_label('最大页数', exact=True).fill('1')
            await page.get_by_label('翻页按钮', exact=False).fill('')
            await page.get_by_label('使用案例 RAG').uncheck()
            async with page.expect_response(lambda r: r.url.endswith('/api/v1/tasks') and r.request.method == 'POST') as response:
                await page.get_by_role('button', name='创建任务', exact=True).click()
            created = await (await response.value).json()
            task_id = created['id']
            print(json.dumps({'task_id': task_id, 'output': str(output)}), flush=True)
            await expect(page.get_by_role('button', name='确认样本并开始采集')).to_be_visible(timeout=150000)
            task = await (await page.request.get(base + '/api/v1/tasks/' + task_id)).json()
            sample = task['summary']['preview']['items']
            assert len(sample) == 3 and set(sample[0]) == {'title', 'price', 'link'}
            assert all(row['link'].startswith('https://books.toscrape.com/') for row in sample)
            await page.screenshot(path=str(output / 'preview.png'), full_page=True)
            await page.get_by_role('button', name='确认样本并开始采集').click()
            await expect(page.locator('.detail-heading .status')).to_have_text('已完成', timeout=120000)
            task = await (await page.request.get(base + '/api/v1/tasks/' + task_id)).json()
            report = task['summary']
            assert report['count'] == 3 and report['pages'] == 1
            assert report['completeness'] == 'partial'
            assert report['model_calls'] >= 1
            for format in ['json', 'csv', 'xlsx']:
                async with page.expect_download() as download:
                    await page.get_by_role('link', name='下载 ' + format.upper(), exact=True).click()
                await (await download.value).save_as(output / ('result.' + format))
            rows = json.loads((output / 'result.json').read_text(encoding='utf-8'))
            assert len(rows) == 3 and all(row['title'] and row['price'] for row in rows)
            assert errors == []
            await page.screenshot(path=str(output / 'result-desktop.png'), full_page=True)
            await page.set_viewport_size({'width': 390, 'height': 844})
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.screenshot(path=str(output / 'result-mobile.png'), full_page=True)
            evidence = {'task_id': task_id, 'report': report, 'output': str(output), 'page_errors': errors}
            (output / 'verification.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(evidence, ensure_ascii=True), flush=True)
        except Exception:
            if task_id:
                task = await (await page.request.get(base + '/api/v1/tasks/' + task_id)).json()
                print(json.dumps({'task_id': task_id, 'status': task['status'], 'summary': task['summary']}, ensure_ascii=True), flush=True)
            await page.screenshot(path=str(output / 'failure.png'), full_page=True)
            raise
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize one task using public network and the configured model')
    parser.add_argument('--base', default='http://127.0.0.1:8002')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required: this verification calls the configured model')
    asyncio.run(verify(args.base.rstrip('/')))
