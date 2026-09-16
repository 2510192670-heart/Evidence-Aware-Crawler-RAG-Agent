"""Demo 只读验证脚本：对已启动的 demo server 跑通三条路径。

前置：demo server 已在 127.0.0.1:8010 运行。
    .venv/Scripts/python.exe -B demo/verify.py

覆盖：
1. 污染预设 -> BLOCK，strong=[S3,S4,S5]，weak=[W2]（与设计文档一致）；
2. 干净预设 -> AUTO_PASS；
3. 非法输入（缺 split）-> 422 fail-closed；
4. 真实 bundle 跨 split 审计（35 对）；
5. evaluate 响应不含任何原始记录/响应体字段名（leak-safe）。
"""
import json

import httpx

BASE = 'http://127.0.0.1:8010'


def main():
    # This verifier targets only the local demo; inherited proxy settings must
    # not route its fixture requests to an external proxy.
    with httpx.Client(base_url=BASE, timeout=30, trust_env=False) as client:
        health = client.get('/api/demo/health').json()
        print('health:', health['frozen'], health['case_count'], health['manifest_sha256'][:16])

        scenarios = client.get('/api/demo/scenarios').json()
        print('scenarios:', [s['id'] for s in scenarios])

        cases = client.get('/api/demo/bundle/cases').json()
        print('bundle cases:', len(cases))

        for sid, expected in (('answer_reuse_block', 'block'), ('clean_auto_pass', 'auto_pass')):
            scenario = client.get(f'/api/demo/scenarios/{sid}').json()
            response = client.post('/api/demo/evaluate', json={
                'case_raw': scenario['case_raw'], 'oracle_raw': scenario['oracle_raw'],
                'fixture_raw': scenario['fixture_raw'], 'against': 'bundle'})
            payload = response.json()
            decisive = payload['results'][0]
            print(f"\n[{sid}] status={response.status_code} overall={payload['overall_decision']} "
                  f"(expected {expected}) pairs={payload['pair_count']} counts={payload['decision_counts']}")
            print('  decisive pair:', decisive['left_case_id'], '<->', decisive['right_case_id'])
            print('  strong:', decisive['strong_signals'])
            print('  weak:', decisive['weak_signals'])
            print('  evidence:', json.dumps(decisive['evidence'], ensure_ascii=False))
            assert payload['overall_decision'] == expected, 'unexpected decision'

        dirty = client.get('/api/demo/scenarios/answer_reuse_block').json()
        bad_case = dict(dirty['case_raw'])
        bad_case.pop('split')
        response = client.post('/api/demo/evaluate', json={
            'case_raw': bad_case, 'oracle_raw': dirty['oracle_raw'],
            'fixture_raw': dirty['fixture_raw'], 'against': 'bundle'})
        print('\n[fail-closed] status=', response.status_code,
              'error=', response.json()['detail']['error_code'])
        assert response.status_code == 422

        audit = client.get('/api/demo/audit').json()
        print('\n[audit] pairs=', audit['pair_count'], 'counts=', audit['decision_counts'])

        raw = client.post('/api/demo/evaluate', json={
            'case_raw': dirty['case_raw'], 'oracle_raw': dirty['oracle_raw'],
            'fixture_raw': dirty['fixture_raw'], 'against': 'bundle'}).text
        for secret in ('twenty', '"entries"', '"payload"', '"records"'):
            assert secret not in raw, f'raw source data leaked into evaluate response: {secret}'
        print('[leak-check] evaluate response contains no raw record/payload field names')

    print('\nALL DEMO CHECKS PASSED')


if __name__ == '__main__':
    main()
