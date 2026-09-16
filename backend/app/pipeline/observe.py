import asyncio
import json
import os
from contextlib import AsyncExitStack
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.async_api import async_playwright
import httpx

from ..policy.enforcement import check_target as policy_check_target
from ..policy.resolution import resolve_and_classify
from ..policy.transport import build_public_transport
from ..policy.robots import fetch_robots
from ..policy.browser import PublicBrowserProxy
from .contracts import Observation, SENSITIVE, has_sensitive_keys, local_origin
from .curl_import import checked_url

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


def route_permitted(url: str, origin: str, policy=None) -> bool:
    """浏览器逐请求拦截的纯判定（提取为函数以便离线矩阵测试）。

    loopback（含 policy=None）：保持原判式 local_origin(url) == origin，逐字节等价。
    public：仅允许与入口 origin 同源——比 allowlist 更严，第三方子资源（CDN/
    统计/字体）一律 fail-closed abort；子请求的 query 是正常形态，判定用去
    query/path 的 origin 形态进行，不套用入口 URL 的 entry-query 限制。
    方法规则（GET / 同源 JSON POST）仍由调用方叠加，本函数不复制。
    """
    try:
        if policy is None or policy.mode == 'loopback':
            return local_origin(url) == origin
        parts = urlsplit(url)
        candidate = urlunsplit((parts.scheme, parts.netloc, '', '', ''))
        return policy_check_target(candidate, policy) == origin
    except ValueError:
        return False


async def observe(url: str, click_text: str | None = '下一页', *, policy=None,
                  resolver=None, html_fallback=False, force_html=False) -> list[Observation]:
    if force_html:
        checked_url(url, policy)
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
    else:
        origin = policy_check_target(url, policy)
    if policy is not None and policy.mode == 'public_http':
        # 公网入口 SSRF 准入：启动浏览器之前完成解析分类（fail-closed，零浏览器副作用）。
        resolve_and_classify(urlsplit(url).hostname, resolver)
    if urlsplit(url).query and not force_html:
        raise ValueError('entry_url_query_not_supported')
    records = []
    pending = []
    markup = None

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
        async with AsyncExitStack() as stack:
            proxy = None
            if policy is not None and policy.mode == 'public_http':
                transport = build_public_transport(resolver=resolver)
                robots = await fetch_robots(origin, transport)
                robots.enforce(urlsplit(url).path or '/')
                client = await stack.enter_async_context(httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False))
                proxy = PublicBrowserProxy(origin, policy, client, robots)
            playwright = await stack.enter_async_context(async_playwright())
            # 浏览器无需继承模型密钥等凭证。
            browser_env = {k: v for k, v in os.environ.items() if not SENSITIVE.search(k)}
            browser = await playwright.chromium.launch(headless=True, env=browser_env)
            try:
                context = await browser.new_context(service_workers='block')

                async def route_request(route):
                    request = route.request
                    permitted = route_permitted(request.url, origin, policy)
                    if permitted:
                        # 边界只放宽到「同源 JSON POST」：表单、multipart 与其他方法继续阻断。
                        permitted = request.method == 'GET' or (
                            request.method == 'POST'
                            and is_json_content_type(header_value(request.headers, 'content-type')))
                    if permitted and proxy is not None:
                        body = request_json_object(request.headers, request.post_data) if request.method == 'POST' else None
                        try:
                            reply = await proxy.fetch(request.url, request.method, body)
                        except (ValueError, httpx.HTTPError, OSError):
                            await route.abort()
                            return
                        await route.fulfill(**reply)
                    elif permitted:
                        await route.continue_()
                    else:
                        await route.abort()

                await context.route('**/*', route_request)
                await context.route_web_socket('**/*', lambda ws: ws.close())
                page = await context.new_page()
                page.on('response', schedule)
                navigation = await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                await asyncio.sleep(1)
                if html_fallback or force_html:
                    markup = await page.content()
                    if len(markup.encode('utf-8')) > 2 * 1024 * 1024:
                        raise ValueError('response_too_large')
                if click_text and not force_html and (not html_fallback or await page.get_by_role('button', name=click_text, exact=True).count()):
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
    if markup is not None and (force_html or not records):
        if navigation is None or not 200 <= navigation.status < 300:
            raise ValueError(f'html_http_status_{navigation.status if navigation else "unknown"}')
        return [Observation(request_id='html_document', url=url, query={}, body={'markup': markup},
                            status=navigation.status)]
    if not records:
        raise ValueError('no_usable_json_requests')
    return records
