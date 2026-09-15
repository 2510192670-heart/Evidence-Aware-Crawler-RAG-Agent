from .errors import TargetPolicyError
from .loader import load_policy_file
from .resolution import Resolver, classify_ip_blocked, resolve_and_classify, socket_resolver
from .robots import RobotsPolicy
from .target import AllowRule, TargetPolicy, check_target, default_policy, policy_sha256

# transport 刻意不在此导出：避免 policy 包级引入 httpx 依赖（C3 接线时按模块导入）。
__all__ = ['AllowRule', 'Resolver', 'RobotsPolicy', 'TargetPolicy', 'TargetPolicyError',
           'check_target', 'classify_ip_blocked', 'default_policy', 'load_policy_file',
           'policy_sha256', 'resolve_and_classify', 'socket_resolver']
