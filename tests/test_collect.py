import httpx
import pytest

from collect import collect_products


def make_client(pages):
    def respond(request):
        page = int(request.url.params['page'])
        return httpx.Response(200, json=pages[page - 1])
    return httpx.Client(transport=httpx.MockTransport(respond), base_url='http://test')


def product(i):
    return {'id': i, 'name': f'商品 {i}', 'price_fen': 1200}


def test_collect_follows_pagination():
    pages = [
        {'page': 1, 'total': 3, 'has_next': True, 'items': [product(1), product(2)]},
        {'page': 2, 'total': 3, 'has_next': False, 'items': [product(3)]},
    ]
    with make_client(pages) as client:
        assert collect_products(client, page_size=2) == [product(1), product(2), product(3)]


@pytest.mark.parametrize('items,total', [([product(1), product(1)], 2), ([product(1)], 2), ([{'id': 1}], 1)])
def test_rejects_duplicates_missing_records_and_invalid_fields(items, total):
    pages = [{'page': 1, 'total': total, 'has_next': False, 'items': items}]
    with make_client(pages) as client, pytest.raises(ValueError):
        collect_products(client)


def test_stops_at_page_limit():
    pages = [{'page': 1, 'total': 2, 'has_next': True, 'items': [product(1)]}]
    with make_client(pages) as client, pytest.raises(ValueError, match='页数上限'):
        collect_products(client, max_pages=1)


def test_http_error_is_not_success():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)), base_url='http://test') as client:
        with pytest.raises(httpx.HTTPStatusError):
            collect_products(client)
