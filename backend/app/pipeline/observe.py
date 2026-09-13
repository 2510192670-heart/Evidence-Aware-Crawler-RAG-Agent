import asyncio
import json
import os
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.async_api import async_playwright

from .contracts import Observation, SENSITIVE, has_sensitive_keys, local_origin

MAX_BODY_BYTES = 256 * 1024
OBSERVED_METHODS = {'GET', 'POST'}


def is_json_content_type(value: str | None) -> bool:
    """只有 application/json（允许 charset 等参数）才算 JSON 请求体。"""
    return bool(value) and value.split(';', 1)[0].strip().lower() == 'application/json'


def header_value(headers, name: str) -> str | None:
    """大小写不敏感地取一个请求头；header 名的大小写由浏览器决定。"""
    lowered = name.lower()
    return next((value for key, value in headers.items() if key.lower() == lowered), None)


def request_json_object(headers, post_data: str | None) -> dict | None:
    """把已观察到的请求体收敛为一个安全 JSON 对象；不合格返回 None。

    任一条件不满足都表示“这条请求不构成可复用证据”，而不是可以放宽的约束：
    非 JSON 请求体、超大、非 UTF-8、顶层不是对象、以及任何敏感键名。
    """
    if not is_json_content_type(header_value(headers, 'content-type')):
        return None
    if post_data is None:
        return None
    try:
        if len(post_data.encode('utf-8')) > MAX_BODY_BYTES:
            return None
    except UnicodeError:
        return None
    try:
        value = json.loads(post_data)
    except ValueError:
        return None
    if not isinstance(value, dict) or not value or has_sensitive_keys(value):
        return None
    return value


async def observe(url: str, click_text: str | None = '下一页') -> list[Observation]:
    origin = local_origin(url)
    if urlsplit(url).query:
        raise ValueError('entry_url_query_not_supported')
    records = []
    pending = []

    async def capture(response):
        request = response.request
        if request.method not in OBSERVED_METHODS or request.resource_type not in {'fetch', 'xhr'}:
            return
        if not 200 <= response.status < 300 or 'json' not in response.headers.get('content-type', ''):
            return
        parts = urlsplit(response.url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = dict(pairs)
        if len(pairs) != len(query) or any(SENSITIVE.search(k) for k in query):
            return
        headers = await request.all_headers()
        if any(SENSITIVE.search(k) for k in headers):
            return
        request_body = None
        if request.method == 'POST':
            request_body = request_json_object(headers, request.post_data)
            if request_body is None:
                return
        try:
            declared = response.headers.get('content-length')
            if declared and int(declared) > MAX_BODY_BYTES:
                return
            # Playwright body API 整体读取；这里限制保留大小，不能声称是网络流量硬上限。
            raw = await response.body()
            if len(raw) > MAX_BODY_BYTES:
                return
            body = json.loads(raw)
            if not isinstance(body, (dict, list)):
                return
        except (ValueError, UnicodeError):
            return
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        records.append(Observation(request_id=f'req_{len(records) + 1:03d}', url=clean_url,
                                   method=request.method, query=query, request_body=request_body,
                                   body=body, status=response.status))

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
                    request = route.request
                    try:
                        permitted = local_origin(request.url) == origin
                    except ValueError:
                        permitted = False
                    if permitted:
                        # 边界只放宽到「同源 JSON POST」：表单、multipart 与其他方法继续阻断。
                        permitted = request.method == 'GET' or (
                            request.method == 'POST'
                            and is_json_content_type(header_value(request.headers, 'content-type')))
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
