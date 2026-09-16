import csv
import io
import zipfile
import xml.etree.ElementTree as ET


def test_csv_preserves_headers_and_blocks_formulas():
    from backend.app.pipeline.result_export import export_csv
    raw = export_csv([{'title': '=HYPERLINK("bad")', 'price': 12, 'missing': None}])
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    assert rows[0] == ['title', 'price', 'missing']
    assert rows[1] == ['\'=HYPERLINK("bad")', '12', '']


def test_xlsx_has_typed_values_no_formulas_and_is_deterministic():
    from backend.app.pipeline.result_export import export_xlsx
    rows = [{'title': '=1+1', 'price': 12.5, 'available': True, 'missing': None}]
    raw = export_xlsx(rows)
    assert raw == export_xlsx(rows)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        assert sheet.findall('.//s:f', ns) == []
        assert sheet.find('.//s:c[@r="A2"]/s:is/s:t', ns).text == '=1+1'
        assert sheet.find('.//s:c[@r="B2"]/s:v', ns).text == '12.5'
        assert sheet.find('.//s:c[@r="C2"]', ns).attrib['t'] == 'b'


def test_export_api_verifies_existing_artifact_hash(tmp_path):
    import json
    import time
    from fastapi.testclient import TestClient
    from backend.app.main import create_app
    from backend.app.llm.config import CloudConfig

    async def worker(spec, config, task_id, root, stage):
        folder = root / 'tasks' / task_id
        folder.mkdir(parents=True)
        (folder / 'result.json').write_text(json.dumps([{'id': 1, 'title': 'A'}]), encoding='utf-8')
        return {'status': 'succeeded', 'count': 1}

    with TestClient(create_app(tmp_path, lambda: CloudConfig('https://model.example', 'fixture', 'fixture'), worker)) as client:
        task = client.post('/api/v1/tasks', json={}).json()
        for _ in range(100):
            row = client.get('/api/v1/tasks/' + task['id']).json()
            if row['status'] == 'succeeded':
                break
            time.sleep(.01)
        base = f"/api/v1/tasks/{task['id']}"
        assert client.get(base + '/result').json()['items'] == [{'id': 1, 'title': 'A'}]
        for format in ['json', 'csv', 'xlsx']:
            assert client.get(base + '/export/' + format).status_code == 200
        (tmp_path / 'tasks' / task['id'] / 'result.json').write_text('[]', encoding='utf-8')
        assert client.get(base + '/export/csv').status_code == 409
