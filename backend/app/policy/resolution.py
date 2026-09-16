"""M7-A S3.3-C1: SSRF 解析守卫。

公网目标的域名在发起任何请求之前必须经过解析 + IP 分类：
- 全部解析结果（A 与 AAAA）都要检查，任一命中危险段即整体拒绝（fail-closed）；
- 未知形态一律视为危险；
- 本模块不发请求、不做重试；resolver 可注入以保证测试完全离线。

C1 边界：仅提供守卫函数，不接入 run/observe/curl_import（接线属 C3/C4）。
"""

import ipaddress
import socket
from typing import Protocol

from .errors import TargetPolicyError

__all__ = ['Resolver', 'classify_ip_blocked', 'resolve_and_classify', 'socket_resolver']

# 版本无关的显式补充段：即便 stdlib is_private 语义随版本变化，这些段也恒定拒绝。
_EXTRA_BLOCKED_V4 = tuple(ipaddress.ip_network(cidr) for cidr in (
    '0.0.0.0/8',          # this-host
    '100.64.0.0/10',      # CGNAT
    '192.0.0.0/24',       # IETF 协议保留
    '192.0.2.0/24',       # TEST-NET-1
    '198.18.0.0/15',      # benchmark
    '198.51.100.0/24',    # TEST-NET-2
    '203.0.113.0/24',     # TEST-NET-3
    '240.0.0.0/4',        # reserved
    '255.255.255.255/32',  # broadcast
))
# 6to4 / Teredo：内嵌 IPv4 可携带私网地址，S3.3 一律整段拒绝（公网目标不应解析到此）。
_EXTRA_BLOCKED_V6 = tuple(ipaddress.ip_network(cidr) for cidr in (
    '2001::/32',          # Teredo
    '2002::/16',          # 6to4
))


class Resolver(Protocol):
    """hostname → IP 字符串列表。空列表表示解析失败。"""

    def __call__(self, hostname: str) -> list: ...


def socket_resolver(hostname: str) -> list:
    """生产 resolver：真实 DNS（A + AAAA），TCP 目标语义。"""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return [info[4][0] for info in infos]


def classify_ip_blocked(value: str) -> bool:
    """单个 IP 是否危险。任何不可解析/未知形态返回 True（fail-closed）。"""
    if not isinstance(value, str):
        return True
    try:
        # IPv6 scope id（fe80::1%eth0）：剥离后再分类，link-local 语义不变。
        ip = ipaddress.ip_address(value.split('%', 1)[0])
    except ValueError:
        return True
    # IPv4-mapped IPv6（::ffff:10.0.0.1）：解包内层 v4 再分类，消除映射绕过面。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return True
    networks = _EXTRA_BLOCKED_V4 if ip.version == 4 else _EXTRA_BLOCKED_V6
    return any(ip in network for network in networks)


def resolve_and_classify(hostname: str, resolver=None) -> tuple:
    """解析并分类；全部 IP 安全才放行，返回规范化 IP 元组（顺序保持）。

    拒绝语义：
    - 解析失败/空结果 → TargetPolicyError('target_resolution_failed')
    - 任一 IP 命中危险段 → TargetPolicyError('ssrf_ip_blocked')
    """
    resolve = resolver if resolver is not None else socket_resolver
    try:
        raw = resolve(hostname)
    except TargetPolicyError:
        raise
    except Exception:
        # resolver 的任何异常都不能变成放行理由。
        raise TargetPolicyError('target_resolution_failed') from None
    if not raw:
        raise TargetPolicyError('target_resolution_failed')
    classified = []
    for value in raw:
        if classify_ip_blocked(value):
            raise TargetPolicyError('ssrf_ip_blocked')
        classified.append(str(ipaddress.ip_address(str(value).split('%', 1)[0])))
    return tuple(classified)
