from fastapi.testclient import TestClient
from backend.app.main import create_app


def test_console_served_without_exposing_project_files(tmp_path):
    site = tmp_path / 'dist'
    site.mkdir()
    (site / 'index.html').write_text('<div id="app"></div>', encoding='utf-8')
    with TestClient(create_app(data_dir=tmp_path / 'data', console_directory=site)) as client:
        response = client.get('/console/')
        assert response.status_code == 200
        assert '<div id="app">' in response.text
        assert client.get('/').url.path == '/console/'
        assert client.get('/console/package.json').status_code == 404
        assert client.get('/api/v1/unknown').status_code == 404


def test_unbuilt_console_falls_back_to_docs(tmp_path):
    with TestClient(create_app(data_dir=tmp_path / 'data', console_directory=tmp_path / 'missing')) as client:
        assert client.get('/').url.path == '/docs'
