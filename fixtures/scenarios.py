"""S1-S5 scenario routers for the multi-structure loopback fixtures.

S1  GET page-number variants (different route, parameter and field names)
S2  POST JSON page-number variants (fixtures only; not executable until M4.3)
S3  nested response containers
S4  distractor clusters: one real paginated interface plus lookalikes
S5  deterministic drift: duplicate key, shifting total, type flip, empty page

All routers share one app, so "multi-candidate" scenarios are genuinely
same-origin. Query parameters are validated the same way the production
learning site validates them, so invalid pagination returns 422 here too.
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .datasets import (
    BLANK_NEXT_PAGES,
    BLANK_NEXT_TOTAL,
    DUPLICATE_PAGES,
    DUPLICATE_TOTAL,
    S1_DEV_1,
    S1_DEV_2,
    S1_HELD_1,
    S1_HELD_2,
    S2_DEV_1,
    S2_DEV_2,
    S2_HELD_1,
    S2_HELD_2,
    S3_DEV_1,
    S3_DEV_2,
    S3_HELD_1,
    S3_HELD_2,
    TOTAL_SHIFT_PAGES,
    TOTAL_SHIFT_VALUES,
    TYPE_FLIP_PAGES,
    TYPE_FLIP_TOTAL,
    has_next_page,
    page_slice,
)

Page = Annotated[int, Query(ge=1)]
Size = Annotated[int, Query(ge=1, le=100)]

s1_router = APIRouter()
s2_router = APIRouter()
s3_router = APIRouter()
s4_router = APIRouter()
s5_router = APIRouter()


# --- S1: GET page-number variants ---------------------------------------

@s1_router.get('/catalog/v1/items')
def s1_dev_items(page: Page = 1, per_page: Size = 10):
    return {'items': page_slice(S1_DEV_1, page, per_page), 'total': len(S1_DEV_1),
            'has_next': has_next_page(S1_DEV_1, page, per_page)}


@s1_router.get('/catalog/v1/search')
def s1_dev_search(p: Page = 1, size: Size = 8):
    return {'rows': page_slice(S1_DEV_2, p, size), 'total': len(S1_DEV_2),
            'more': has_next_page(S1_DEV_2, p, size)}


@s1_router.get('/inventory/v2/lines')
def s1_held_lines(page_no: Page = 1, page_len: Size = 12):
    return {'entries': page_slice(S1_HELD_1, page_no, page_len), 'record_total': len(S1_HELD_1),
            'more_pages': has_next_page(S1_HELD_1, page_no, page_len)}


@s1_router.get('/inventory/v2/ledger')
def s1_held_ledger(pg: Page = 1, idx: Size = 6):
    return {'lines': page_slice(S1_HELD_2, pg, idx), 'count_total': len(S1_HELD_2),
            'next_flag': has_next_page(S1_HELD_2, pg, idx)}


# --- S2: POST JSON page-number variants ---------------------------------

class _S2Dev1Body(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page: int = Field(1, ge=1)
    page_size: int = Field(10, ge=1, le=100)


class _S2Dev2Body(BaseModel):
    model_config = ConfigDict(extra='forbid')
    p: int = Field(1, ge=1)
    size: int = Field(10, ge=1, le=100)


class _S2Held1Body(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page_no: int = Field(1, ge=1)
    page_len: int = Field(8, ge=1, le=100)


class _S2Held2Body(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pg: int = Field(1, ge=1)
    idx: int = Field(6, ge=1, le=100)


@s2_router.post('/catalog/v1/query')
def s2_dev_query(body: _S2Dev1Body):
    return {'items': page_slice(S2_DEV_1, body.page, body.page_size), 'total': len(S2_DEV_1),
            'has_next': has_next_page(S2_DEV_1, body.page, body.page_size)}


@s2_router.post('/catalog/v1/query-rows')
def s2_dev_query_rows(body: _S2Dev2Body):
    return {'rows': page_slice(S2_DEV_2, body.p, body.size), 'total': len(S2_DEV_2),
            'more': has_next_page(S2_DEV_2, body.p, body.size)}


@s2_router.post('/inventory/v2/lookup')
def s2_held_lookup(body: _S2Held1Body):
    return {'entries': page_slice(S2_HELD_1, body.page_no, body.page_len), 'record_total': len(S2_HELD_1),
            'more_pages': has_next_page(S2_HELD_1, body.page_no, body.page_len)}


@s2_router.post('/inventory/v2/browse')
def s2_held_browse(body: _S2Held2Body):
    return {'lines': page_slice(S2_HELD_2, body.pg, body.idx), 'count_total': len(S2_HELD_2),
            'next_flag': has_next_page(S2_HELD_2, body.pg, body.idx)}


# --- S3: nested response containers -------------------------------------

@s3_router.get('/catalog/v1/nested')
def s3_dev_nested(page: Page = 1, page_size: Size = 10):
    return {'data': {'records': page_slice(S3_DEV_1, page, page_size), 'totalCount': len(S3_DEV_1),
                     'hasMore': has_next_page(S3_DEV_1, page, page_size)}}


@s3_router.get('/catalog/v1/nested-total')
def s3_dev_nested_total(page: Page = 1, page_size: Size = 7):
    return {'data': {'records': page_slice(S3_DEV_2, page, page_size), 'totalCount': len(S3_DEV_2)}}


@s3_router.get('/inventory/v2/grouped')
def s3_held_grouped(page_no: Page = 1, page_len: Size = 10):
    return {'payload': {'entries': page_slice(S3_HELD_1, page_no, page_len), 'total_records': len(S3_HELD_1),
                        'has_more': has_next_page(S3_HELD_1, page_no, page_len)}}


@s3_router.get('/inventory/v2/grouped-summary')
def s3_held_grouped_summary(page_idx: Page = 1, batch_len: Size = 5):
    return {'payload': {'entries': page_slice(S3_HELD_2, page_idx, batch_len), 'total_records': len(S3_HELD_2)}}


# --- S4: distractor clusters --------------------------------------------

@s4_router.get('/catalog/v1/items-meta')
def s4_dev_items_meta(page: Page = 1):
    # Lookalike route name, but it never returns an item list.
    return {'endpoint': 'items-meta', 'page': page, 'note': 'no item list is served here'}


@s4_router.get('/catalog/v1/status')
def s4_dev_status():
    return {'service': 'catalog', 'revision': 3, 'healthy': True}


@s4_router.get('/catalog/v1/search-facets')
def s4_dev_search_facets(p: Page = 1):
    return {'facets': [{'code': 1, 'label': '分类 A', 'hits': 4}], 'page': p}


@s4_router.get('/catalog/v1/health')
def s4_dev_health():
    return {'ready': True, 'checks': []}


@s4_router.get('/inventory/v2/lines-preview')
def s4_held_lines_preview(page_no: Page = 1):
    # Reuses the real container key but is a fixed preview, not pagination.
    return {'entries': S1_HELD_1[:3], 'preview': True, 'requested_page': page_no}


@s4_router.get('/inventory/v2/meta')
def s4_held_meta():
    return {'service': 'inventory', 'revision': 7}


@s4_router.get('/inventory/v2/ledger-summary')
def s4_held_ledger_summary(pg: Page = 1):
    return {'lines': [{'entry_id': 0, 'summary': '聚合行', 'value': 5000}],
            'aggregate': True, 'requested_page': pg}


@s4_router.get('/inventory/v2/probe')
def s4_held_probe():
    return {'ready': True, 'build': 'fixture-1'}


# --- S5: deterministic drift --------------------------------------------

@s5_router.get('/catalog/v1/duplicate-keys')
def s5_dev_duplicate(page: Page = 1, page_size: Size = 10):
    return {'items': DUPLICATE_PAGES.get(page, []), 'total': DUPLICATE_TOTAL, 'has_next': page < 2}


@s5_router.get('/catalog/v1/total-shift')
def s5_dev_total_shift(page: Page = 1, page_size: Size = 10):
    return {'rows': TOTAL_SHIFT_PAGES.get(page, []), 'total': TOTAL_SHIFT_VALUES.get(page, 0),
            'more': page < 2}


@s5_router.get('/inventory/v2/type-flip')
def s5_held_type_flip(page_no: Page = 1, page_len: Size = 10):
    return {'entries': TYPE_FLIP_PAGES.get(page_no, []), 'record_total': TYPE_FLIP_TOTAL,
            'more_pages': page_no < 2}


@s5_router.get('/inventory/v2/blank-next')
def s5_held_blank_next(pg_no: Page = 1, pg_len: Size = 10):
    return {'lines': BLANK_NEXT_PAGES.get(pg_no, []), 'count_total': BLANK_NEXT_TOTAL, 'next_flag': True}


ROUTERS = (s1_router, s2_router, s3_router, s4_router, s5_router)
