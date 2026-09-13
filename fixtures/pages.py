"""Entry pages for the loopback fixtures.

Benchmark tasks point at a page URL, not directly at a JSON endpoint, because
the real pipeline starts from browser observation. Each page issues the same
XHR/fetch requests a human learner would trigger, so observation sees a
genuine same-origin request set (S4 pages deliberately fire the distractors as
well).

Pages carry no random content, so what a page emits is fixed.
"""

import json

_TEMPLATE = '''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>__TITLE__</title>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <p id="status" role="status">加载中…</p>
  <pre id="out"></pre>
  <button id="next" __NEXT_DISABLED__>下一页</button>
</main>
<script>
  const LOAD = __LOAD__;
  const NEXT = __NEXT__;
  async function call(spec) {
    const options = { method: spec.method };
    if (spec.body) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(spec.body);
    }
    const query = spec.params ? '?' + new URLSearchParams(spec.params).toString() : '';
    const response = await fetch(spec.path + query, options);
    return { url: spec.path + query, status: response.status, body: await response.json() };
  }
  async function run(calls) {
    const results = [];
    for (const spec of calls) { results.push(await call(spec)); }
    document.getElementById('out').textContent = JSON.stringify(results, null, 2);
    document.getElementById('status').textContent = '已加载 ' + results.length + ' 个请求';
  }
  document.getElementById('next').addEventListener('click', () => run(NEXT));
  run(LOAD);
</script>
</body>
</html>
'''


def _get(path, **params):
    return {'method': 'GET', 'path': path, 'params': params}


def _post(path, payload):
    return {'method': 'POST', 'path': path, 'body': payload}


def _page(title, calls, next_calls=()):
    return {'title': title, 'calls': list(calls), 'next': list(next_calls)}


PAGE_SPECS = {
    's1-dev-1': _page('S1 变体 1 · GET /catalog/v1/items',
                      [_get('/catalog/v1/items', page=1, per_page=10)],
                      [_get('/catalog/v1/items', page=2, per_page=10)]),
    's1-dev-2': _page('S1 变体 2 · GET /catalog/v1/search',
                      [_get('/catalog/v1/search', p=1, size=8)],
                      [_get('/catalog/v1/search', p=2, size=8)]),
    's1-held-1': _page('S1 留出 1 · GET /inventory/v2/lines',
                       [_get('/inventory/v2/lines', page_no=1, page_len=12)],
                       [_get('/inventory/v2/lines', page_no=2, page_len=12)]),
    's1-held-2': _page('S1 留出 2 · GET /inventory/v2/ledger',
                       [_get('/inventory/v2/ledger', pg=1, idx=6)],
                       [_get('/inventory/v2/ledger', pg=2, idx=6)]),
    's2-dev-1': _page('S2 变体 1 · POST /catalog/v1/query',
                      [_post('/catalog/v1/query', {'page': 1, 'page_size': 10})],
                      [_post('/catalog/v1/query', {'page': 2, 'page_size': 10})]),
    's2-dev-2': _page('S2 变体 2 · POST /catalog/v1/query-rows',
                      [_post('/catalog/v1/query-rows', {'p': 1, 'size': 10})],
                      [_post('/catalog/v1/query-rows', {'p': 2, 'size': 10})]),
    's2-held-1': _page('S2 留出 1 · POST /inventory/v2/lookup',
                       [_post('/inventory/v2/lookup', {'page_no': 1, 'page_len': 8})],
                       [_post('/inventory/v2/lookup', {'page_no': 2, 'page_len': 8})]),
    's2-held-2': _page('S2 留出 2 · POST /inventory/v2/browse',
                       [_post('/inventory/v2/browse', {'pg': 1, 'idx': 6})],
                       [_post('/inventory/v2/browse', {'pg': 2, 'idx': 6})]),
    's3-dev-1': _page('S3 变体 1 · GET /catalog/v1/nested',
                      [_get('/catalog/v1/nested', page=1, page_size=10)],
                      [_get('/catalog/v1/nested', page=2, page_size=10)]),
    's3-dev-2': _page('S3 变体 2 · GET /catalog/v1/nested-total',
                      [_get('/catalog/v1/nested-total', page=1, page_size=7)],
                      [_get('/catalog/v1/nested-total', page=2, page_size=7)]),
    's3-held-1': _page('S3 留出 1 · GET /inventory/v2/grouped',
                       [_get('/inventory/v2/grouped', page_no=1, page_len=10)],
                       [_get('/inventory/v2/grouped', page_no=2, page_len=10)]),
    's3-held-2': _page('S3 留出 2 · GET /inventory/v2/grouped-summary',
                       [_get('/inventory/v2/grouped-summary', page_idx=1, batch_len=5)],
                       [_get('/inventory/v2/grouped-summary', page_idx=2, batch_len=5)]),
    's4-dev-1': _page('S4 变体 1 · 目录簇（1 个真实分页 + 2 个干扰）',
                      [_get('/catalog/v1/items', page=1, per_page=10),
                       _get('/catalog/v1/items-meta', page=1),
                       _get('/catalog/v1/status')],
                      [_get('/catalog/v1/items', page=2, per_page=10)]),
    's4-dev-2': _page('S4 变体 2 · 检索簇（1 个真实分页 + 2 个干扰）',
                      [_get('/catalog/v1/search', p=1, size=8),
                       _get('/catalog/v1/search-facets', p=1),
                       _get('/catalog/v1/health')],
                      [_get('/catalog/v1/search', p=2, size=8)]),
    's4-held-1': _page('S4 留出 1 · 物料簇（1 个真实分页 + 2 个干扰）',
                       [_get('/inventory/v2/lines', page_no=1, page_len=12),
                        _get('/inventory/v2/lines-preview', page_no=1),
                        _get('/inventory/v2/meta')],
                       [_get('/inventory/v2/lines', page_no=2, page_len=12)]),
    's4-held-2': _page('S4 留出 2 · 台账簇（1 个真实分页 + 2 个干扰）',
                       [_get('/inventory/v2/ledger', pg=1, idx=6),
                        _get('/inventory/v2/ledger-summary', pg=1),
                        _get('/inventory/v2/probe')],
                       [_get('/inventory/v2/ledger', pg=2, idx=6)]),
    's5-dev-1': _page('S5 变体 1 · GET /catalog/v1/duplicate-keys',
                      [_get('/catalog/v1/duplicate-keys', page=1, page_size=10)],
                      [_get('/catalog/v1/duplicate-keys', page=2, page_size=10)]),
    's5-dev-2': _page('S5 变体 2 · GET /catalog/v1/total-shift',
                      [_get('/catalog/v1/total-shift', page=1, page_size=10)],
                      [_get('/catalog/v1/total-shift', page=2, page_size=10)]),
    's5-held-1': _page('S5 留出 1 · GET /inventory/v2/type-flip',
                       [_get('/inventory/v2/type-flip', page_no=1, page_len=10)],
                       [_get('/inventory/v2/type-flip', page_no=2, page_len=10)]),
    's5-held-2': _page('S5 留出 2 · GET /inventory/v2/blank-next',
                       [_get('/inventory/v2/blank-next', pg_no=1, pg_len=10)],
                       [_get('/inventory/v2/blank-next', pg_no=2, pg_len=10)]),
}


def render_page(name):
    spec = PAGE_SPECS[name]
    return (_TEMPLATE
            .replace('__NEXT_DISABLED__', '' if spec['next'] else 'disabled')
            .replace('__TITLE__', spec['title'])
            .replace('__LOAD__', json.dumps(spec['calls'], ensure_ascii=False))
            .replace('__NEXT__', json.dumps(spec['next'], ensure_ascii=False)))
