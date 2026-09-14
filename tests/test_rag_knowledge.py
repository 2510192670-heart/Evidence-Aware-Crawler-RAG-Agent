"""M5.2-A: read-only Case Knowledge v2 loader and schema validator.

Covers legacy loading, v2 parsing, fail-closed validation (unknown code,
category/phase/repairability mismatch), the security categories never projecting
to ``auto_applied``, non-mutation, determinism and the architectural boundary
(taxonomy + read-only policy only; no execution/retrieval pipeline import).
"""

import ast
import copy
import json
from pathlib import Path

import pytest

from backend.app.pipeline.errors import SPECS, Category
from backend.app.rag import knowledge


def v2_case():
    return {'id': 'k1', 'title': 'POST 翻页失败知识', 'terms': ['page', 'items', 'data'],
            'guidance': 'hint only',
            'scenario': {'method': 'POST', 'page_location': 'json_body', 'stop_signal': 'boolean_flag'},
            'failure_modes': [
                {'code': 'pointer_not_found', 'category': 'PLAN_SEMANTIC', 'phase': 'analyzing',
                 'signal': 'pointer_absent'},
                {'code': 'page_location_mismatch', 'category': 'PLAN_SEMANTIC', 'phase': 'analyzing',
                 'signal': 'method_mismatch'}],
            'repairability': {'pointer_not_found': 'auto_applied', 'page_location_mismatch': 'proposal_only'}}


def mutated(mutate):
    case = copy.deepcopy(v2_case())
    mutate(case)
    return case


def code_of(excinfo):
    return excinfo.value.code


# --- 1. legacy case can be loaded ---------------------------------------

def test_legacy_corpus_loads_as_schema_version_1():
    view = knowledge.load()
    raw = json.loads(Path(knowledge.__file__).with_name('cases.json').read_bytes())
    assert view and [case.id for case in view] == [case['id'] for case in raw]
    for case in view:
        assert case.case_schema_version == 1
        assert case.failure_modes == ()
        assert dict(case.scenario) == {}
        assert dict(case.repairability) == {}
        assert isinstance(case.terms, tuple)


# --- 2. v2 case is parsed correctly -------------------------------------

def test_v2_case_is_parsed_and_projection_matches():
    case = knowledge.validate_case(v2_case())
    assert case.case_schema_version == 2
    assert dict(case.scenario) == {'method': 'POST', 'page_location': 'json_body',
                                   'stop_signal': 'boolean_flag'}
    assert [mode.code for mode in case.failure_modes] == ['pointer_not_found', 'page_location_mismatch']
    assert case.failure_modes[0].category == 'PLAN_SEMANTIC'
    assert case.failure_modes[0].phase == 'analyzing'
    assert dict(case.repairability) == {'pointer_not_found': 'auto_applied',
                                        'page_location_mismatch': 'proposal_only'}
    assert knowledge.project_repairability('pointer_not_found') == knowledge.AUTO_APPLIED
    assert knowledge.project_repairability('page_location_mismatch') == knowledge.PROPOSAL_ONLY


# --- 3-6. schema validation fails closed --------------------------------

def test_unknown_error_code_fails():
    with pytest.raises(knowledge.KnowledgeError) as excinfo:
        knowledge.validate_case(mutated(lambda case: case['failure_modes'][0].update(code='not_registered')))
    assert code_of(excinfo) == 'unknown_error_code'


def test_category_mismatch_fails():
    with pytest.raises(knowledge.KnowledgeError) as excinfo:
        knowledge.validate_case(mutated(lambda case: case['failure_modes'][0].update(category='SECURITY')))
    assert code_of(excinfo) == 'error_category_mismatch'


def test_phase_mismatch_fails():
    with pytest.raises(knowledge.KnowledgeError) as excinfo:
        knowledge.validate_case(mutated(lambda case: case['failure_modes'][0].update(phase='executing')))
    assert code_of(excinfo) == 'error_phase_mismatch'


def test_repairability_mismatch_fails():
    with pytest.raises(knowledge.KnowledgeError) as excinfo:
        knowledge.validate_case(mutated(
            lambda case: case['repairability'].update(pointer_not_found='rejected')))
    assert code_of(excinfo) == 'repairability_mismatch'


# --- 7. security categories can never project to auto_applied -----------

def test_restricted_categories_never_project_to_auto_applied():
    restricted = {Category.SECURITY, Category.DATA_INTEGRITY, Category.TRANSPORT}
    codes = [code for code, spec in SPECS.items() if spec.category in restricted]
    assert codes, 'the taxonomy must still contain restricted categories'
    for code in codes:
        assert knowledge.project_repairability(code) == knowledge.REJECTED
    # UNCLASSIFIED 不是注册项：未注册码一律 fail closed，绝不可能是 auto_applied。
    assert not [code for code, spec in SPECS.items() if spec.category is Category.UNCLASSIFIED]
    with pytest.raises(knowledge.KnowledgeError) as excinfo:
        knowledge.project_repairability('unregistered_code')
    assert code_of(excinfo) == 'unknown_error_code'
    # 案例也不能把受限失败声明成 auto_applied。
    with pytest.raises(knowledge.KnowledgeError) as excinfo:
        knowledge.validate_case({
            'id': 'bad', 'title': 't', 'terms': [], 'guidance': '',
            'failure_modes': [{'code': 'invalid_url', 'category': 'SECURITY', 'phase': 'observing',
                               'signal': 'target_not_allowed'}],
            'repairability': {'invalid_url': 'auto_applied'}})
    assert code_of(excinfo) == 'repairability_mismatch'


# --- 8. the input is never mutated --------------------------------------

def test_input_corpus_is_not_mutated():
    corpus = [v2_case(), {'id': 'legacy', 'title': 't', 'terms': ['a'], 'guidance': 'g'}]
    before = json.loads(json.dumps(corpus))
    view = knowledge.load(cases=corpus)
    assert corpus == before
    # 返回的视图也不得是可变别名。
    with pytest.raises(TypeError):
        view[0].scenario['method'] = 'GET'


# --- 9. identical input is deterministic --------------------------------

def test_same_input_is_deterministic():
    first = knowledge.load(cases=[v2_case()])
    second = knowledge.load(cases=[v2_case()])
    assert first == second
    assert knowledge.project_repairability('pointer_not_found') == knowledge.AUTO_APPLIED


# --- 10. no execution/retrieval pipeline import -------------------------

def test_module_imports_stay_within_the_read_only_boundary():
    tree = ast.parse(Path(knowledge.__file__).read_text(encoding='utf-8'))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append('.' * node.level + (node.module or ''))
    joined = ' '.join(modules)
    for forbidden in ('execution', 'retrieval', 'run', 'httpx', 'openai', 'requests', 'playwright'):
        assert forbidden not in joined, forbidden
    # 只允许 taxonomy 与只读策略常量，外加共享的 feature 词汇表。
    assert sorted(module for module in modules if module.startswith('.')) == [
        '..pipeline.errors', '..pipeline.repair', '.features']
