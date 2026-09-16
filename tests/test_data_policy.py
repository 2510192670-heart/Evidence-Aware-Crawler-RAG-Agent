"""M7 Governance Layer：DataPolicy 数据边界判定测试。

覆盖：公开数据允许 / 敏感字段拒绝 / 绕过行为拒绝 / 默认策略安全 /
错误码稳定 / 与 TargetPolicy 独立 / purpose 治理元数据可追溯。
"""
import inspect

import pytest
from fastapi.testclient import TestClient

from backend.app.llm.config import CloudConfig
from backend.app.main import create_app
from backend.app.policy import TargetPolicyError
from backend.app.policy.data_policy import (_FORBIDDEN_RULES, DENIAL_CODE, PURPOSES,
                                            DataClassification, DataDecision,
                                            admit_intent, decide, evaluate_intent)
from backend.app.policy.errors import DataPolicyError


# --- 1. 公开数据允许 -------------------------------------------------------

@pytest.mark.parametrize('description,purpose', [
    ('分析公开商品价格趋势', None),
    ('分析公开新闻数据', 'research'),
    ('collect public product listings', 'analysis'),
    ('', None),
])
def test_public_intent_allowed(description, purpose):
    decision = evaluate_intent(description, purpose)
    assert decision.classification is DataClassification.PUBLIC
    assert decision.allowed is True
    assert decision.code is None and decision.reason is None
    assert admit_intent(description, purpose) == decision


# --- 2. 敏感字段拒绝（个人身份信息 / 隐私数据） ----------------------------

@pytest.mark.parametrize('description,reason', [
    ('收集用户手机号', 'possible_personal_data_collection'),
    ('抓取身份证号和银行卡信息', 'possible_personal_data_collection'),
    ('整理私人地址与家庭住址', 'possible_personal_data_collection'),
    ('collect phone number and id card data', 'possible_personal_data_collection'),
    ('导出用户列表', 'privacy_data_collection'),
    ('收集私人账号与聊天记录', 'privacy_data_collection'),
    ('build a contact list of private accounts', 'privacy_data_collection'),
])
def test_sensitive_data_rejected(description, reason):
    decision = evaluate_intent(description)
    assert decision.classification is DataClassification.SENSITIVE
    assert decision.allowed is False
    assert decision.reason == reason
    with pytest.raises(DataPolicyError) as error:
        admit_intent(description)
    assert str(error.value) == DENIAL_CODE
    assert error.value.reason == reason


def test_sensitive_field_name_rejected():
    """中文字段名绕过 SENSITIVE 英文正则时，DataPolicy 仍然拒绝。"""
    decision = evaluate_intent('采集公开商品数据', fields=['id', '手机号'])
    assert decision.allowed is False
    assert decision.classification is DataClassification.SENSITIVE
    assert decision.matched_rule == 'personal_identity'


# --- 3. 绕过行为拒绝 -------------------------------------------------------

@pytest.mark.parametrize('description', [
    '破解目标网站的反爬机制',
    '绕过登录抓取后台数据',
    '自动识别验证码继续采集',
    'cookie盗取后重放请求',
    'bypass login and crack the captcha',
])
def test_circumvention_rejected(description):
    decision = evaluate_intent(description)
    assert decision.classification is DataClassification.RESTRICTED
    assert decision.allowed is False
    assert decision.reason == 'circumvention_not_allowed'
    assert decision.matched_rule == 'circumvention'
    with pytest.raises(DataPolicyError) as error:
        admit_intent(description)
    assert error.value.reason == 'circumvention_not_allowed'


# --- 4. 默认策略安全（fail-closed） ----------------------------------------

def test_default_decision_is_public_and_safe():
    decision = evaluate_intent()
    assert decision.classification is DataClassification.PUBLIC
    assert decision.allowed is True
    assert decision.purpose is None and decision.matched_rule is None


def test_decision_model_is_frozen_and_closed():
    decision = evaluate_intent('分析公开新闻数据', 'research')
    assert decision.schema_version == 1
    with pytest.raises(Exception):
        decision.allowed = False
    with pytest.raises(Exception):
        DataDecision(classification='PUBLIC', allowed=True, unknown_key=1)


def test_unknown_purpose_rejected():
    with pytest.raises(ValueError) as error:
        evaluate_intent('分析公开新闻数据', 'resell_personal_data')
    assert str(error.value) == 'unsupported_purpose'


def test_authorized_requires_explicit_authorization():
    """AUTHORIZED 默认拒绝；只有显式授权才放行（v1 无授权通道 → fail-closed）。"""
    assert decide(DataClassification.AUTHORIZED) == (False, 'authorization_required')
    assert decide(DataClassification.AUTHORIZED, authorized=True) == (True, None)
    assert decide(DataClassification.PUBLIC) == (True, None)
    assert decide(DataClassification.SENSITIVE)[0] is False
    assert decide(DataClassification.RESTRICTED)[0] is False


def test_decision_never_echoes_user_input():
    decision = evaluate_intent('收集用户手机号和身份证')
    dumped = decision.model_dump(mode='json')
    assert '手机号' not in str(dumped) and '身份证' not in str(dumped)
    assert dumped['matched_rule'] == 'personal_identity'


# --- 5. 错误码稳定 ---------------------------------------------------------

def test_error_code_is_stable_contract():
    assert DENIAL_CODE == 'data_policy_restricted'
    with pytest.raises(DataPolicyError) as error:
        admit_intent('破解验证码')
    # str(error) 恰为稳定码；code 属性一致；reason 是封闭词汇。
    assert str(error.value) == 'data_policy_restricted'
    assert error.value.code == 'data_policy_restricted'
    assert error.value.reason in {'circumvention_not_allowed',
                                  'possible_personal_data_collection',
                                  'privacy_data_collection',
                                  'authorization_required',
                                  'restricted_data_not_supported'}


def test_denial_reasons_are_closed_vocabulary():
    reasons = {rule[2] for rule in _FORBIDDEN_RULES}
    assert reasons == {'circumvention_not_allowed', 'possible_personal_data_collection',
                       'privacy_data_collection'}


# --- 6. 与 TargetPolicy 独立 -----------------------------------------------

def test_data_policy_is_independent_of_target_policy():
    """DataPolicy 不消费 URL/allowlist，不抛 TargetPolicyError，签名无 policy 参数。"""
    assert not issubclass(DataPolicyError, TargetPolicyError)
    assert not issubclass(TargetPolicyError, DataPolicyError)
    signature = inspect.signature(evaluate_intent)
    assert 'policy' not in signature.parameters
    # 目标判定与数据判定互不替代：公网 URL 不影响数据分类结果。
    assert evaluate_intent('https://example.com 分析公开商品价格趋势').allowed is True
    assert evaluate_intent('https://example.com 收集用户手机号').allowed is False


# --- API 集成：admission 顺序、结构化 422、治理元数据可追溯 ----------------

def config():
    return CloudConfig('https://model.example', 'test-secret', 'test-model')


async def worker(spec, config, task_id, root, stage):
    return {'status': 'succeeded', 'count': 0}


def test_api_restricted_intent_rejected_before_persistence(tmp_path):
    with TestClient(create_app(tmp_path, config, worker)) as client:
        response = client.post('/api/v1/tasks', json={
            'url': 'http://127.0.0.1:8000/', 'fields': ['id'],
            'description': '收集用户手机号'})
        assert response.status_code == 422
        body = response.json()
        assert body['code'] == 'data_policy_restricted'
        assert body['reason'] == 'possible_personal_data_collection'
        assert client.get('/api/v1/tasks').json()['total'] == 0


def test_api_circumvention_rejected_with_reason(tmp_path):
    with TestClient(create_app(tmp_path, config, worker)) as client:
        response = client.post('/api/v1/tasks', json={
            'url': 'http://127.0.0.1:8000/', 'fields': ['id'],
            'description': '绕过登录限制抓取数据'})
        assert response.status_code == 422
        assert response.json()['code'] == 'data_policy_restricted'
        assert response.json()['reason'] == 'circumvention_not_allowed'


def test_api_purpose_and_decision_persisted_in_spec(tmp_path):
    """治理决策与 purpose 随 tasks.spec 持久化：可追溯，不只在内存判断。"""
    with TestClient(create_app(tmp_path, config, worker)) as client:
        response = client.post('/api/v1/tasks', json={
            'url': 'http://127.0.0.1:8000/', 'fields': ['id'],
            'description': '分析公开新闻数据', 'purpose': 'research'})
        assert response.status_code == 202, response.json()
        spec = response.json()['spec']
        assert spec['purpose'] == 'research'
        assert spec['data_policy']['classification'] == 'PUBLIC'
        assert spec['data_policy']['allowed'] is True
        assert spec['data_policy']['purpose'] == 'research'
        stored = client.get(f"/api/v1/tasks/{response.json()['id']}").json()
        assert stored['spec']['data_policy']['schema_version'] == 1


def test_api_unknown_purpose_rejected(tmp_path):
    with TestClient(create_app(tmp_path, config, worker)) as client:
        response = client.post('/api/v1/tasks', json={
            'url': 'http://127.0.0.1:8000/', 'fields': ['id'], 'purpose': 'resell'})
        assert response.status_code == 422
        assert response.json()['code'] == 'invalid_request'


def test_api_data_policy_does_not_leak_into_target_rejections(tmp_path):
    """目标拒绝仍走目标判定通道：两层策略互不污染。"""
    with TestClient(create_app(tmp_path, config, worker)) as client:
        response = client.post('/api/v1/tasks', json={
            'url': 'https://example.com/', 'fields': ['id'],
            'description': '分析公开商品价格趋势'})
        assert response.status_code == 422
        # 无 policy 引用的公网 URL 在 TaskInput 结构预检即拒绝，与 DataPolicy 无关。
        assert response.json()['code'] == 'invalid_request'
        assert 'reason' not in response.json()


def test_purposes_vocabulary_is_closed():
    assert PURPOSES == {'research', 'analysis', 'monitoring', 'archiving', 'other'}
