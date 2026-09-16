"""M7-A S3.3-C1: robots 检查接口（纯判定，无 IO，未接线）。

职责边界：
- 本模块不抓取 robots.txt——抓取属 C3/C4 接线层，且抓取请求本身必须过
  check_target + resolve_and_classify（守卫顺序不可颠倒）；
- 输入是"抓取结果"（成功与否 + 文本），输出是 allow/disallow 判定；
- fail-closed：未抓取 / 抓取失败 / 非法文本 → 一律视为不允许。

语义约定（与 RFC 9309 惯例一致的最小实现）：
- 抓取成功且 robots.txt 不存在（HTTP 404，调用方传 fetch_succeeded=True,
  robots_text=''）→ 无规则 = 允许；
- 抓取成功且有规则 → 按 stdlib robotparser 判定；
- 其余一切情况 → 拒绝（robots_disallowed）。

C1 边界：不接入 observe/run。
"""

import urllib.robotparser

from .errors import TargetPolicyError

__all__ = ['RobotsPolicy']


class RobotsPolicy:
    """单个 origin 的 robots 判定器；不可变，构造后只读。"""

    def __init__(self, robots_text=None, *, fetch_succeeded: bool = False):
        parser = None
        if fetch_succeeded:
            if robots_text is None:
                robots_text = ''
            if not isinstance(robots_text, str):
                # 非法文本形态 = 不可判定 → fail-closed。
                robots_text = None
            if robots_text is not None:
                parser = urllib.robotparser.RobotFileParser()
                try:
                    parser.parse(robots_text.splitlines())
                except Exception:
                    parser = None
        self._parser = parser

    @property
    def fetch_succeeded(self) -> bool:
        return self._parser is not None

    def is_allowed(self, path: str, user_agent: str = '*') -> bool:
        if self._parser is None:
            return False
        if not isinstance(path, str) or not path.startswith('/'):
            # 只判定绝对路径形态；其他形态不可信。
            return False
        return self._parser.can_fetch(user_agent, path)

    def enforce(self, path: str, user_agent: str = '*'):
        if not self.is_allowed(path, user_agent):
            raise TargetPolicyError('robots_disallowed')


# Shared by task orchestration and the guarded browser observer.
import httpx

ROBOTS_MAX_BYTES = 64 * 1024

async def fetch_robots(origin: str, transport) -> RobotsPolicy:
    """抓取 robots.txt：经守卫 transport（每请求重解析分类）；fail-closed。

    404 = 无 robots 文件 = 无规则（RFC 9309 惯例，允许）；其他非 200、网络
    异常、非法文本一律拒绝。安全类异常（如 ssrf_ip_blocked）不在此捕获，
    以稳定码上抛。
    """
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     transport=transport) as client:
            async with client.stream('GET', origin + '/robots.txt', timeout=10) as response:
                if response.status_code == 404:
                    return RobotsPolicy('', fetch_succeeded=True)
                if response.status_code != 200:
                    return RobotsPolicy()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > ROBOTS_MAX_BYTES:
                        return RobotsPolicy()
                    raw.extend(chunk)
    except (httpx.HTTPError, OSError):
        return RobotsPolicy()
    try:
        return RobotsPolicy(raw.decode('utf-8'),
                            fetch_succeeded=True)
    except UnicodeDecodeError:
        return RobotsPolicy()
