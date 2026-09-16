"""M6.1-B2 面试展示 Demo：evaluation.overlap 的只读展示层。

边界（与设计方案一致）：
- 只调用 evaluation 层的公开纯函数与只读冻结守卫，不修改 evaluation/、
  backend/、frontend/ 的任何文件；
- 启动时先跑 assert_m61a_frozen，冻结校验失败则拒绝启动（fail-closed）；
- evaluate 响应经过白名单断言：只允许 digest/计数/relation 出网，
  oracle 记录、响应体原文永不出现在判决结果里；
- 场景预设（/scenarios/{id}）返回的是"作者输入"，其中污染场景按定义
  包含被复用的答案数据——那是待检输入，不是检测器输出，两者边界不同；
- 零 LLM 调用、零随机性、零持久化。

运行（仓库根目录）：
    .venv/Scripts/python.exe -m uvicorn demo.server:app --host 127.0.0.1 --port 8010
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from evaluation.overlap import (STRONG_SIGNALS, WEAK_SIGNALS, OverlapDecision,
                                audit_cross_split, compare, load_m6_v1_views,
                                normalize_case)
from evaluation.schemas.contract_v2 import assert_m61a_frozen
from evaluation.schemas.hashing import digest

ROOT = Path(__file__).resolve().parents[1]
BASE = 'evaluation/datasets/m6-v1/'
# 污染场景的答案复用目标：一个 frozen_evaluation 侧的 success case。
REUSE_TARGET = 'm6_get_nested'

# ---------------------------------------------------------------------------
# 启动即冻结校验（只读）。任何篡改 -> 异常 -> 应用拒绝启动。
# ---------------------------------------------------------------------------
FROZEN_CASE_COUNT = assert_m61a_frozen(ROOT)
MANIFEST_SHA256 = (ROOT / (BASE + 'manifest.sha256')).read_text(encoding='utf-8').strip()
VIEWS = load_m6_v1_views(ROOT)
VIEWS_BY_ID = {view.case_id: view for view in VIEWS}


def _read_json(ref):
    """只读 evaluation/ 下的 JSON；路径逃逸或不存在直接 fail-closed。"""
    if not isinstance(ref, str) or not ref.startswith('evaluation/'):
        raise ValueError('invalid_relative_reference')
    path = (ROOT / ref).resolve()
    if not path.is_relative_to((ROOT / 'evaluation').resolve()) or not path.is_file():
        raise ValueError('missing_or_escaping_reference')
    return json.loads(path.read_text(encoding='utf-8'))


_FAMILIES = {family['id']: family for family in _read_json(BASE + 'families.json')}


def _family_signature(family_id):
    """输入 case 声明的 family 若在注册表中，用注册表签名（同 family 即同签名，
    S2 才有机会命中）；否则用 demo 专属占位签名，保证不与任何冻结 family 相等。"""
    family = _FAMILIES.get(family_id)
    if family is not None:
        return family['signature']
    return {'demo_only_family': family_id}


# ---------------------------------------------------------------------------
# 预设场景：全部从冻结 bundle 派生，保证 digest 与被复用目标严格一致。
# ---------------------------------------------------------------------------
def _build_scenarios():
    target_case_ref = BASE + 'cases/' + REUSE_TARGET + '.json'
    target_case = _read_json(target_case_ref)
    target_oracle = _read_json(target_case['oracle']['ref'])
    target_fixture = _read_json(target_case['fixture']['ref'])
    target_responses = target_fixture['endpoints'][0]['responses']

    # 场景 1（BLOCK）：候选 case 复用了 frozen 侧 case 的 oracle 记录与响应 payload。
    # 端点改为 POST 且不带任何分页键，使 W3/W4 不命中——最终 strong=[S3,S4,S5]、
    # weak=[W2]，与设计文档中的展示目标一致。
    dirty_records = target_oracle['records']
    dirty = {
        'id': 'answer_reuse_block',
        'name': '污染候选：复用冻结侧答案（预期 BLOCK）',
        'expected_decision': 'block',
        'description': 'oracle 记录、result hash 与响应 payload 均复制自 '
                       + REUSE_TARGET + '；family 与请求形态刻意不同。',
        'case_raw': {
            'id': 'demo_answer_reuse', 'split': 'development',
            'family_id': 'demo_answer_reuse',
            'repair_expectation': {'policy_disposition': 'none'},
        },
        'oracle_raw': {
            'records': dirty_records,
            'result_sha256': target_oracle['result_sha256'],
            'expected_error_code': None,
        },
        'fixture_raw': {
            'endpoints': [{
                'method': 'POST', 'query': None, 'request_body': None,
                'responses': target_responses,
            }],
        },
    }

    # 场景 2（AUTO_PASS）：完全原创的候选 case，与冻结 bundle 无任何信号命中。
    clean_records = [{'sku': 'DEMO-0001', 'label': 'demo-only-row-alpha'},
                     {'sku': 'DEMO-0002', 'label': 'demo-only-row-beta'}]
    clean = {
        'id': 'clean_auto_pass',
        'name': '干净候选：全新结构与数据（预期 AUTO_PASS）',
        'expected_decision': 'auto_pass',
        'description': '原创记录、原创响应形状、无分页键；与 7 个冻结 case 逐一比对。',
        'case_raw': {
            'id': 'demo_clean_candidate', 'split': 'development',
            'family_id': 'demo_clean_family',
            'repair_expectation': {'policy_disposition': 'none'},
        },
        'oracle_raw': {
            'records': clean_records,
            'result_sha256': digest(clean_records),
            'expected_error_code': None,
        },
        'fixture_raw': {
            'endpoints': [{
                'method': 'GET', 'query': None, 'request_body': None,
                'responses': [
                    {'payload': {'rows': [clean_records[0]], 'marker': 'demo-page-alpha'}},
                    {'payload': {'rows': [clean_records[1]], 'marker': 'demo-page-beta'}},
                ],
            }],
        },
    }
    return {item['id']: item for item in (dirty, clean)}


SCENARIOS = _build_scenarios()

# ---------------------------------------------------------------------------
# evaluate 响应白名单：出现任何白名单外的键即 500，把 leak-safe 从设计承诺
# 变成运行时可验证。
# ---------------------------------------------------------------------------
_ALLOWED_KEYS = frozenset({
    'ok', 'overall_decision', 'pair_count', 'decision_counts', 'results',
    'left_case_id', 'right_case_id', 'decision', 'strong_signals', 'weak_signals',
    'evidence', 'relation', 'digest', 'count', 'left_view_summary',
    'case_id', 'split', 'family_id', 'family_signature_digest',
    'oracle_result_sha256', 'record_hash_count', 'payload_hash_count',
    'subtree_hash_count', 'endpoint_composition_signature', 'pagination_signature',
    'failure_code', 'repair_disposition', 'leak_guard',
    'raw_records_returned', 'raw_payloads_returned',
    # decision_counts 的键是三态判决值本身；evidence 的键是检测器信号 ID，
    # 直接绑定 overlap 模块导出的常量，不复制字符串。
    'block', 'manual_review', 'auto_pass',
}) | frozenset(STRONG_SIGNALS) | frozenset(WEAK_SIGNALS)


def _assert_leak_safe(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in _ALLOWED_KEYS:
                raise RuntimeError('leak_guard_violation:' + str(key))
            _assert_leak_safe(value)
    elif isinstance(node, list):
        for item in node:
            _assert_leak_safe(item)


_DECISION_RANK = {OverlapDecision.AUTO_PASS: 0, OverlapDecision.MANUAL_REVIEW: 1,
                OverlapDecision.BLOCK: 2}

app = FastAPI(title='Overlap Detector Playground', docs_url=None, redoc_url=None)


class EvaluateRequest(BaseModel):
    case_raw: dict
    oracle_raw: dict
    fixture_raw: dict
    against: str = 'bundle'


def _view_summary(view):
    return {
        'case_id': view.case_id, 'split': view.split, 'family_id': view.family_id,
        'family_signature_digest': view.family_signature_digest,
        'oracle_result_sha256': view.oracle_result_sha256,
        'record_hash_count': len(view.record_hashes),
        'payload_hash_count': len(view.response_payload_hashes),
        'subtree_hash_count': len(view.nested_subtree_hashes),
        'endpoint_composition_signature': view.endpoint_composition_signature,
        'pagination_signature': view.pagination_signature,
        'failure_code': view.failure_code,
        'repair_disposition': view.repair_disposition,
    }


@app.get('/api/demo/health')
def health():
    return {'status': 'ok', 'bundle': 'm6-v1', 'manifest_sha256': MANIFEST_SHA256,
            'frozen': True, 'case_count': FROZEN_CASE_COUNT}


@app.get('/api/demo/scenarios')
def list_scenarios():
    return [{'id': item['id'], 'name': item['name'],
             'expected_decision': item['expected_decision'],
             'description': item['description']} for item in SCENARIOS.values()]


@app.get('/api/demo/scenarios/{scenario_id}')
def get_scenario(scenario_id: str):
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail='unknown_scenario')
    return {'id': scenario['id'], 'name': scenario['name'],
            'expected_decision': scenario['expected_decision'],
            'description': scenario['description'],
            'case_raw': scenario['case_raw'], 'oracle_raw': scenario['oracle_raw'],
            'fixture_raw': scenario['fixture_raw'], 'against': 'bundle'}


@app.get('/api/demo/bundle/cases')
def bundle_cases():
    """冻结 bundle 目录：只有 id/split/family 三个字段，不含任何答案数据。"""
    return [{'case_id': view.case_id, 'split': view.split, 'family_id': view.family_id}
            for view in sorted(VIEWS, key=lambda v: v.case_id)]


@app.post('/api/demo/evaluate')
def evaluate(request: EvaluateRequest):
    try:
        family_id = request.case_raw.get('family_id')
        view = normalize_case(request.case_raw, request.oracle_raw, request.fixture_raw,
                              _family_signature(family_id))
    except ValueError as error:
        # fail-closed：非法输入直接拒绝并报错误码，绝不降级成 PASS。
        raise HTTPException(status_code=422,
                            detail={'ok': False, 'error_code': str(error),
                                    'note': 'detector refuses malformed input '
                                            'rather than degrading to a pass'})
    if request.against == 'bundle':
        targets = [other for other in VIEWS if other.case_id != view.case_id]
    elif request.against in VIEWS_BY_ID:
        targets = [VIEWS_BY_ID[request.against]]
    else:
        raise HTTPException(status_code=422,
                            detail={'ok': False, 'error_code': 'unknown_against_target'})
    results = [compare(view, target) for target in targets]
    results.sort(key=lambda result: (-_DECISION_RANK[result.decision],
                                     result.left_case_id, result.right_case_id))
    overall = max((result.decision for result in results),
                  key=lambda decision: _DECISION_RANK[decision],
                  default=OverlapDecision.AUTO_PASS)
    counts = {}
    for result in results:
        counts[result.decision.value] = counts.get(result.decision.value, 0) + 1
    payload = {
        'ok': True,
        'overall_decision': overall.value,
        'pair_count': len(results),
        'decision_counts': counts,
        'results': [result.to_jsonable() for result in results],
        'left_view_summary': _view_summary(view),
        'leak_guard': {'raw_records_returned': False, 'raw_payloads_returned': False},
    }
    _assert_leak_safe(payload)
    return payload


@app.get('/api/demo/audit')
def audit():
    """真实冻结 bundle 的跨 split 全量审计（35 对），只读。"""
    results = audit_cross_split(VIEWS)
    counts = {}
    for result in results:
        counts[result.decision.value] = counts.get(result.decision.value, 0) + 1
    payload = {'ok': True, 'pair_count': len(results), 'decision_counts': counts,
               'results': [result.to_jsonable() for result in results]}
    _assert_leak_safe(payload)
    return payload


app.mount('/', StaticFiles(directory=Path(__file__).resolve().parent / 'static',
                           html=True), name='demo-static')
