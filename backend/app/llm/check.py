"""DeepSeek Flash 连通性检查；只有 --live 才发送一次真实请求。"""

import argparse
import asyncio
from dataclasses import asdict
from getpass import getpass
import json
import os

from pydantic import BaseModel, ConfigDict

from .config import CloudConfig
from .gateway import CloudGateway, GatewayError


class CheckResult(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    ok: bool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='发送一次真实 API 请求，可能产生费用')
    args = parser.parse_args()
    if not args.live:
        print('离线模式：未发送请求。DeepSeek Flash 预设已提供。')
        print('真实验证：python -m backend.app.llm.check --live（缺少密钥时会隐藏输入）。')
        return
    values = dict(os.environ)
    if not values.get('WDA_LLM_API_KEY') and not values.get('DEEPSEEK_API_KEY'):
        values['WDA_LLM_API_KEY'] = getpass('请输入 DeepSeek API Key（隐藏输入，不保存）：')
    try:
        config = CloudConfig.deepseek_from_env(values)
        result = asyncio.run(CloudGateway(config).generate(
            {'purpose': 'connectivity_check', 'expected_output': {'ok': True}}, CheckResult,
        ))
        if not result.data.ok:
            parser.exit(1, 'API 已响应，但检查结果不是 ok=true。\n')
    except ValueError:
        parser.exit(1, '模型配置无效，请检查 CLOUD_API_SETUP.md 中的环境变量。\n')
    except GatewayError as error:
        parser.exit(1, f'API 检查失败：{error.code}；未自动重试。\n')
    print(json.dumps({'status': 'passed', 'model': result.model,
                      'usage': asdict(result.usage), 'elapsed_seconds': round(result.elapsed_seconds, 2)},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
