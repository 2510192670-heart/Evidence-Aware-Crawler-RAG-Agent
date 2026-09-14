"""Fixed datasets served by the multi-structure loopback fixtures.

Every value is derived from the record index only: no randomness, no clock and
no process state. Fixture responses are therefore reproducible byte for byte,
which is what lets the frozen benchmark manifest stay valid.

Drift datasets are deliberately inconsistent. They exist so that the executor's
deterministic failure modes can be reproduced on demand, so they must never be
"fixed".
"""


def make_records(count, *, id_field, text_field, number_field, prefix):
    """Build ``count`` records with a stable integer id, a text and an amount."""
    return [{id_field: index, text_field: f'{prefix} {index:02d}', number_field: 1000 + index * 100}
            for index in range(1, count + 1)]


def page_slice(records, page, page_size):
    start = (page - 1) * page_size
    return records[start:start + page_size]


def has_next_page(records, page, page_size):
    return page * page_size < len(records)


# --- S1: GET page-number variants ---------------------------------------

S1_DEV_1 = make_records(25, id_field='sku', text_field='title', number_field='amount_fen', prefix='样品')
S1_DEV_2 = make_records(24, id_field='code', text_field='label', number_field='price', prefix='条目')
S1_HELD_1 = make_records(32, id_field='article_no', text_field='description', number_field='net_price_fen', prefix='物料')
S1_HELD_2 = make_records(18, id_field='entry_id', text_field='summary', number_field='value', prefix='台账')

# --- S2: POST JSON page-number variants (not executable until M4.3) ------

S2_DEV_1 = make_records(20, id_field='sku', text_field='title', number_field='amount_fen', prefix='样品')
S2_DEV_2 = make_records(30, id_field='code', text_field='label', number_field='price', prefix='条目')
S2_HELD_1 = make_records(24, id_field='article_no', text_field='description', number_field='net_price_fen', prefix='物料')
S2_HELD_2 = make_records(12, id_field='entry_id', text_field='summary', number_field='value', prefix='台账')

# --- S3: nested response variants ---------------------------------------

S3_DEV_1 = make_records(30, id_field='sku', text_field='title', number_field='amount_fen', prefix='样品')
S3_DEV_2 = make_records(21, id_field='code', text_field='label', number_field='price', prefix='条目')
S3_HELD_1 = make_records(26, id_field='article_no', text_field='description', number_field='net_price_fen', prefix='物料')
S3_HELD_2 = make_records(14, id_field='entry_id', text_field='summary', number_field='value', prefix='台账')

# S4 reuses the S1 datasets: the "correct" interface of a distractor cluster
# is a real paginated interface, so its answer key is the S1 answer key.

# --- S5: deterministic drift payloads ------------------------------------

# Page 2 repeats sku=2, so the unique-key check must abort on page 2.
DUPLICATE_PAGES = {
    1: [{'sku': 1, 'title': '复用 01', 'amount_fen': 1100},
        {'sku': 2, 'title': '复用 02', 'amount_fen': 1200}],
    2: [{'sku': 2, 'title': '复用 02', 'amount_fen': 1200},
        {'sku': 3, 'title': '复用 03', 'amount_fen': 1300}],
}
DUPLICATE_TOTAL = 3

# ``total`` differs between pages, so the total-consistency check must abort.
TOTAL_SHIFT_PAGES = {
    1: [{'code': 1, 'label': '漂移 01', 'price': 1100},
        {'code': 2, 'label': '漂移 02', 'price': 1200}],
    2: [{'code': 3, 'label': '漂移 03', 'price': 1300}],
}
TOTAL_SHIFT_VALUES = {1: 4, 2: 5}

# ``net_price_fen`` is an integer on page 1 and a string on page 2.
TYPE_FLIP_PAGES = {
    1: [{'article_no': 1, 'description': '类型 01', 'net_price_fen': 1100}],
    2: [{'article_no': 2, 'description': '类型 02', 'net_price_fen': '1100'}],
}
TYPE_FLIP_TOTAL = 2

# An empty page keeps ``next_flag`` true, so the stop condition must abort.
BLANK_NEXT_PAGES = {
    1: [{'entry_id': 1, 'summary': '空页 01', 'value': 1100}],
    2: [],
}
BLANK_NEXT_TOTAL = 2
