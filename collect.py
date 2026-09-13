"""请求分页接口，验证数据后保存 JSON。先启动 main.py 对应的服务。"""

import argparse
import json
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field


class Product(BaseModel):
    model_config = ConfigDict(strict=True)
    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    price_fen: int = Field(ge=0)


class ProductPage(BaseModel):
    model_config = ConfigDict(strict=True)
    page: int = Field(gt=0)
    total: int = Field(ge=0)
    has_next: bool
    items: list[Product]


def collect_products(client: httpx.Client, page_size: int = 10, max_pages: int = 100) -> list[dict]:
    """只认识测试站的接口约定；不是任意网站通用爬虫。"""
    if not 1 <= page_size <= 100 or max_pages < 1:
        raise ValueError('page_size 应为 1～100，max_pages 应大于 0')
    result = []
    seen_ids = set()
    expected_total = None
    for page in range(1, max_pages + 1):
        response = client.get('/api/products', params={'page': page, 'page_size': page_size})
        response.raise_for_status()
        data = ProductPage.model_validate(response.json())
        if data.page != page:
            raise ValueError('响应页码与请求不一致')
        if expected_total is None:
            expected_total = data.total
        if data.total != expected_total:
            raise ValueError('采集过程中商品总数发生变化，请重新采集')
        if len(data.items) > page_size:
            raise ValueError('响应条数超过请求的每页条数')
        for item in data.items:
            if item.id in seen_ids:
                raise ValueError(f'发现重复商品 ID：{item.id}')
            seen_ids.add(item.id)
            result.append(item.model_dump())
        if len(result) > expected_total:
            raise ValueError('采集条数超过接口声明的总数')
        if not data.has_next:
            if len(result) != expected_total:
                raise ValueError(f'数据不完整：预期 {expected_total} 条，实际 {len(result)} 条')
            return result
        if not data.items:
            raise ValueError('接口声明还有下一页，但本页为空')
    raise ValueError(f'达到页数上限 {max_pages}，采集尚未结束')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--page-size', type=int, default=10)
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'data' / 'products.json')
    args = parser.parse_args()
    try:
        with httpx.Client(base_url=args.base_url, timeout=10, trust_env=False) as client:
            products = collect_products(client, page_size=args.page_size)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(products, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (httpx.HTTPError, ValueError, OSError) as error:
        parser.exit(1, f'采集失败：{error}\n请确认测试站已启动，且地址和端口正确。\n')
    print(f'采集完成：{len(products)} 条，ID 无重复，字段与总数校验通过。')
    print(f'保存位置：{args.output.resolve()}')


if __name__ == '__main__':
    main()
