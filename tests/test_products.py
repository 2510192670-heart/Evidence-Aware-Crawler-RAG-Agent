from fastapi.testclient import TestClient
import pytest

from main import app

client = TestClient(app)


def test_homepage_is_html():
    response = client.get('/')
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    assert '商品分页学习站' in response.text


def test_three_pages_are_complete_and_unique():
    items = []
    for page in range(1, 4):
        response = client.get('/api/products', params={'page': page, 'page_size': 10})
        assert response.status_code == 200
        data = response.json()
        assert data['total'] == 30
        assert data['page'] == page
        assert len(data['items']) == 10
        assert data['has_next'] is (page < 3)
        items.extend(data['items'])
    assert [item['id'] for item in items] == list(range(1, 31))
    assert all(item['name'] and item['price_fen'] > 0 for item in items)


def test_last_partial_page_and_empty_page():
    data = client.get('/api/products?page=5&page_size=7').json()
    assert [item['id'] for item in data['items']] == [29, 30]
    assert data['has_next'] is False
    data = client.get('/api/products?page=6&page_size=7').json()
    assert data['items'] == []
    assert data['has_next'] is False


@pytest.mark.parametrize('query', ['page=0', 'page=-1', 'page=abc', 'page_size=0', 'page_size=101'])
def test_invalid_pagination_is_rejected(query):
    assert client.get('/api/products?' + query).status_code == 422
