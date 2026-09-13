"""第 1 课：用固定数据理解 HTTP 接口与分页。"""

from typing import Annotated
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

app = FastAPI(title="商品分页学习站", version="0.1.0")


@app.get("/", response_class=FileResponse)
def homepage():
    return FileResponse(Path(__file__).parent / "static" / "index.html")

# 固定的测试数据；金额单位为分，例如 1990 表示 19.90 元。
PRODUCTS = [
    {"id": i, "name": f"学习商品 {i:02d}", "price_fen": 1000 + i * 100}
    for i in range(1, 31)
]


@app.get("/api/products")
def list_products(
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 10,
):
    # 第 2 页、每页 10 条：start=10，end=20，取 ID 11～20。
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "page": page,
        "page_size": page_size,
        "total": len(PRODUCTS),
        "has_next": end < len(PRODUCTS),
        "items": PRODUCTS[start:end],
    }
