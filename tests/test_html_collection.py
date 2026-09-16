import asyncio
import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def test_document_budget_blocks_next_list_page_before_observation(monkeypatch):
    from types import SimpleNamespace
    from backend.app.pipeline import html
    from backend.app.pipeline.requirements import FieldSpec
    calls = []

    async def observe(url, *args, **kwargs):
        calls.append(url)
        return [SimpleNamespace(body={'markup': url})]

    async def parse(markup, plan, *, detail=False):
        if detail:
            return {'values': {'title': markup}}
        return {'rows': [{'values': {}, 'detail': f'/detail/{i}'} for i in range(59)],
                'next': '/page2'}

    monkeypatch.setattr(html, 'observe', observe)
    monkeypatch.setattr(html, 'parse_dom', parse)
    plan = html.HtmlPlan(record_selector='article', detail_link_selector='a', next_selector='a.next',
                         fields={'title': {'selector': 'h1', 'source': 'detail'}})
    with pytest.raises(ValueError, match='html_request_budget_exceeded'):
        asyncio.run(html.execute_html('http://127.0.0.1/', plan, [FieldSpec(name='title')],
                                      max_pages=2, max_records=100))
    assert len(calls) == 60
    assert 'http://127.0.0.1/page2' not in calls


def test_outline_keeps_attributes_from_later_matching_elements():
    from backend.app.pipeline.html import parse_dom
    outline = asyncio.run(parse_dom('<body><a href="/">Home</a><article><a href="/book" title="Full title">Short</a></article></body>'))
    link = next(node for node in outline['nodes'] if node['selector'] == 'a')
    assert 'title' in link['attributes']


def test_list_summary_does_not_follow_inadmissible_detail_candidate(monkeypatch):
    from types import SimpleNamespace
    from backend.app.pipeline import html
    async def no_observation(*args, **kwargs):
        pytest.fail('inadmissible candidate must never be fetched')
    monkeypatch.setattr(html, 'observe', no_observation)
    record = SimpleNamespace(url='http://127.0.0.1/', body={
        'markup': '<body><article><a href="https://external.example/item">Title</a></article></body>'})
    summary = asyncio.run(html.html_summary(record))
    assert summary['nodes']
    assert 'detail_nodes' not in summary


def test_http_error_document_cannot_be_collected_as_data():
    from backend.app.pipeline.html import HtmlPlan, execute_html
    from backend.app.pipeline.requirements import FieldSpec
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<article><h2>Temporarily unavailable</h2></article>')

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        plan = HtmlPlan(record_selector='article', fields={'title': {'selector': 'h2'}})
        with pytest.raises(ValueError, match='html_http_status_503'):
            asyncio.run(execute_html(f'http://127.0.0.1:{server.server_port}/', plan,
                                      [FieldSpec(name='title')]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_rendered_list_and_detail_collection_is_bounded_and_sourced(tmp_path, monkeypatch):
    from backend.app.pipeline.html import HtmlPlan, execute_html
    from backend.app.pipeline.requirements import FieldSpec
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            pages = {
                '/': '<article class="book"><a href="/one">Book one</a></article><a class="next" href="/page2">Next</a>',
                '/page2': '<article class="book"><a href="/two">Book two</a></article>',
                '/one': '<p class="author">Author one</p>',
                '/two': '<p>No author available</p>',
            }
            body = ('<html><body>' + pages.get(self.path, '') + '</body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    plan = HtmlPlan(record_selector='article.book', detail_link_selector='a', next_selector='a.next',
                    fields={'title': {'selector': 'a'}, 'author': {'selector': '.author', 'source': 'detail'}})
    specs = [FieldSpec(name='title', type='string'), FieldSpec(name='author', type='string', required=False)]
    try:
        result = asyncio.run(execute_html(url, plan, specs, max_pages=2, max_records=10))
        assert result['items'] == [{'title': 'Book one', 'author': 'Author one'},
                                   {'title': 'Book two', 'author': None}]
        assert result['pages'] == 2
        assert result['completeness'] == 'stop_condition_only'
        assert result['source_urls'] == [url + 'one', url + 'two']
        # Full pipeline with real HTTP/browser/extraction and one deterministic
        # model fixture. This verifies orchestration, not model intelligence.
        import json
        import uuid
        from types import SimpleNamespace
        from backend.app.pipeline import run
        previews = []

        class Gateway:
            def __init__(self, config):
                self.calls, self.usage_history = 0, []

            async def generate(self, payload, schema):
                assert schema is HtmlPlan
                assert payload['observations'][0]['detail_nodes']
                self.calls += 1
                return SimpleNamespace(data=plan)

        async def preview(value):
            previews.append(value)

        monkeypatch.setattr(run, 'CloudGateway', Gateway)
        args = SimpleNamespace(url=url, fields='title,author', max_pages=2, max_records=10,
                               click_text='', source_mode='auto', preview=True, rag_enabled=False,
                               description='Extract book titles and authors',
                               field_specs=[spec.model_dump() for spec in specs])
        task_id = str(uuid.uuid4())
        code = asyncio.run(run.run_task(args, SimpleNamespace(model='fixture'), task_id=task_id,
                                      output_root=tmp_path, quiet=True, on_preview=preview))
        report = json.loads((tmp_path / 'tasks' / task_id / 'report.json').read_text())
        assert code == 0, report
        assert report['count'] == 2 and report['model_calls'] == 1
        assert report['missing_fields'] == {'title': 0, 'author': 1}
        assert report['collection_mode'] == 'html'
        assert len(previews) == 1 and previews[0]['items'][0]['author'] == 'Author one'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
