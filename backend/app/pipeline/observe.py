import asyncio
import json
import os
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.async_api import async_playwright

from .contracts import Observation, SENSITIVE, local_origin


async def observe(url: str, click_text: str | None = '下一页') -> list[Observation]:
    origin = local_origin(url)
    if urlsplit(url).query:
        raise ValueError('entry_url_query_not_supported')
    records = []
    pending = []

    async def capture(response):
        if response.request.method != 'GET' or response.request.resource_type not in {'fetch', 'xhr'}:
            return
        if not 200 <= response.status < 300 or 'json' not in response.headers.get('content-type', ''):
            return
        parts = urlsplit(response.url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = dict(pairs)
        if len(pairs) != len(query) or any(SENSITIVE.search(k) for k in query):
            return
        headers = await response.request.all_headers()
        if any(SENSITIVE.search(k) for k in headers):
            return
        try:
            declared = response.headers.get('content-length')
            if declared and int(declared) > 256 * 1024:
                return
            # Playwright body API 整体读取；这里限制保留大小，不能声称是网络流量硬上限。
            raw = await response.body()
            if len(raw) > 256 * 1024:
                return
            body = json.loads(raw)
            if not isinstance(body, (dict, list)):
                return
        except (ValueError, UnicodeError):
            return
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        records.append(Observation(request_id=f'req_{len(records) + 1:03d}', url=clean_url,
                                   query=query, body=body, status=response.status))

    def schedule(response):
        if len(pending) < 20:
            pending.append(asyncio.create_task(capture(response)))

    async with asyncio.timeout(40):
        async with async_playwright() as playwright:
            # 浏览器无需继承模型密钥等凭证。
            browser_env = {k: v for k, v in os.environ.items() if not SENSITIVE.search(k)}
            browser = await playwright.chromium.launch(headless=True, env=browser_env)
            try:
                context = await browser.new_context(service_workers='block')

                async def route_request(route):
                    try:
                        permitted = local_origin(route.request.url) == origin and route.request.method == 'GET'
                    except ValueError:
                        permitted = False
                    if permitted:
                        await route.continue_()
                    else:
                        await route.abort()

                await context.route('**/*', route_request)
                await context.route_web_socket('**/*', lambda ws: ws.close())
                page = await context.new_page()
                page.on('response', schedule)
                await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                await asyncio.sleep(1)
                if click_text:
                    await page.get_by_role('button', name=click_text, exact=True).click(timeout=10000)
                    await asyncio.sleep(1)
                page.remove_listener('response', schedule)
                if pending:
                    await asyncio.gather(*pending)
            finally:
                for task in pending:
                    if not task.done():
                        task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await browser.close()
    if not records:
        raise ValueError('no_usable_json_requests')
    return records
