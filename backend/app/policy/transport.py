"""M7-A S3.3-C1: 公网传输层接口（最小实现，未接线）。

为 C3 的 public execution 提供 transport 构造点：
- 每请求前重解析 + 重分类（软 pinning，压缩 DNS rebinding 窗口）；
- 每域保守限速（固定最小间隔，默认 2 rps）。

C1 边界：只建立接口与最小实现，不接入 run.py、不修改 execution；
loopback 路径永远不构造本 transport（client 现状不变）。
本模块刻意不在 policy/__init__ 导出，避免包级引入 httpx 依赖。
"""

import asyncio
import time

import httpx

from .errors import TargetPolicyError
from .resolution import resolve_and_classify

__all__ = ['PublicTransport', 'build_public_transport']

DEFAULT_PER_DOMAIN_RPS = 2.0
MAX_PER_DOMAIN_RPS = 2.0


class PublicTransport(httpx.AsyncHTTPTransport):
    """请求前守卫 + 每域限速的异步 transport。

    守卫顺序（先安全后配额）：重解析分类 → 限速等待 → 交给 httpx 连接。
    任何守卫失败都发生在连接建立之前，请求不会离开本机。
    """

    def __init__(self, resolver=None, per_domain_rps: float = DEFAULT_PER_DOMAIN_RPS,
                 clock=time.monotonic, **kwargs):
        super().__init__(**kwargs)
        if not 0 < per_domain_rps <= MAX_PER_DOMAIN_RPS:
            raise ValueError('per_domain_rps_out_of_range')
        self._resolver = resolver
        self._min_interval = 1.0 / per_domain_rps
        self._clock = clock
        self._last_request: dict = {}
        self._locks: dict = {}

    def pre_request_checks(self, host: str) -> tuple:
        """重解析 + 重分类（纯守卫，可离线单测）；返回规范化 IP 元组。"""
        if not isinstance(host, str) or not host:
            raise TargetPolicyError('ssrf_ip_blocked')
        return resolve_and_classify(host, self._resolver)

    async def _throttle(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock()
            wait = self._last_request.get(host, 0.0) + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = self._clock()

    async def handle_async_request(self, request):
        host = request.url.host
        self.pre_request_checks(host)
        await self._throttle(host)
        return await super().handle_async_request(request)


def build_public_transport(resolver=None,
                           per_domain_rps: float = DEFAULT_PER_DOMAIN_RPS) -> PublicTransport:
    """C3 接线点：run.py 在 public 模式用其构造 httpx.AsyncClient(transport=…)。"""
    return PublicTransport(resolver=resolver, per_domain_rps=per_domain_rps)
