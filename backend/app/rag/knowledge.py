"""Read-only Case Knowledge v2 loader and schema validator (M5.2-A).

This module loads structured case knowledge, validates it against the frozen
failure taxonomy and projects repairability deterministically. It is additive:
``backend/app/rag/cases.json`` still loads unchanged as schema version 1, and
nothing here changes retrieval behaviour or artifacts.

Design boundaries:

* the failure taxonomy in ``pipeline/errors.py`` is the single source of truth
  for categories, phases and repairability; this module reads the registry and
  never re-declares policy,
* the effective disposition (``auto_applied`` / ``proposal_only`` / ``rejected``)
  is *projected* from ``errors.SPECS`` plus the read-only repair policy
  constants (``APPLICABLE_ERROR_CODES``, ``PRE_COLLECTION_PHASES``); a case may
  declare a disposition but only to have it checked, never to set policy,
* the scenario vocabulary is reused from ``features.py`` so no second feature
  vocabulary exists,
* validation is fail closed: anything inconsistent raises :class:`KnowledgeError`,
* no I/O beyond reading the frozen corpus, no HTTP, no model call, and no import
  of the execution or retrieval pipeline.

The projection is a policy view, not an execution path: it never authorizes a
repair and never calls the bounded-repair driver.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
import re

from ..pipeline.errors import SPECS
from ..pipeline.repair import APPLICABLE_ERROR_CODES, PRE_COLLECTION_PHASES
from .features import (AMBIGUITY, CONTINUATION_FLAGS, FEATURE_KEYS, FIELD_MAPPINGS,
                       ITEMS_CONTAINERS, METHODS, PAGE_LOCATIONS, STOP_SIGNALS, UNKNOWN)

__all__ = ['AUTO_APPLIED', 'DISPOSITIONS', 'FAILURE_MODE_KEYS', 'KnowledgeCase', 'KnowledgeError',
           'PROPOSAL_ONLY', 'REJECTED', 'SCENARIO_KEYS', 'SCENARIO_VOCABULARY', 'SIGNAL_PATTERN',
           'FailureMode', 'load', 'project_repairability', 'validate_case']

# 可修复性投影的闭集取值。prefixed 语义：auto_applied 表示 M4.4 允许自动应用，
# proposal_only 表示只生成候选，rejected 表示策略拒绝。
AUTO_APPLIED = 'auto_applied'
PROPOSAL_ONLY = 'proposal_only'
REJECTED = 'rejected'
DISPOSITIONS = (AUTO_APPLIED, PROPOSAL_ONLY, REJECTED)

# 触发 v2 解析的可选字段；一个都不出现时该 case 视为 schema version 1。
V2_KEYS = ('scenario', 'failure_modes', 'repairability')
FAILURE_MODE_KEYS = ('code', 'category', 'phase', 'signal')

# 场景词汇表直接取自 features.py，禁止新增第二套 feature vocabulary。
SCENARIO_KEYS = FEATURE_KEYS
SCENARIO_VOCABULARY = {
    'method': METHODS,
    'page_location': PAGE_LOCATIONS,
    'items_container': ITEMS_CONTAINERS,
    'stop_signal': STOP_SIGNALS,
    'continuation_flag': CONTINUATION_FLAGS,
    'field_mapping': FIELD_MAPPINGS,
    'ambiguity': AMBIGUITY,
}

# signal 只允许闭集形式的短标识（小写字母/数字/下划线），不是自由文本，也不得携带值。
MAX_SIGNAL_LENGTH = 64
SIGNAL_PATTERN = re.compile(r'[a-z][a-z0-9_]{0,63}')


class KnowledgeError(ValueError):
    """Fail-closed knowledge validation failure; ``str`` is exactly the code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise KnowledgeError(code)


@dataclass(frozen=True)
class FailureMode:
    """One failure a case is knowledge about; ``category``/``phase`` come from the registry."""

    code: str
    category: str
    phase: str
    signal: str


@dataclass(frozen=True)
class KnowledgeCase:
    """Immutable, read-only view of one case.

    Legacy fields (``id``/``title``/``terms``/``guidance``) are always present.
    ``scenario``/``failure_modes``/``repairability`` are empty for a schema
    version 1 case. Mappings are read-only proxies of freshly built dicts, so the
    loaded view can never mutate its source.
    """

    id: str
    title: str
    terms: tuple
    guidance: str
    case_schema_version: int
    scenario: object
    failure_modes: tuple
    repairability: object


def project_repairability(code):
    """Deterministically project the effective repair disposition of an error code.

    Derived only from the taxonomy registry and the read-only repair policy
    constants: a non-repairable code, or one outside the pre-collection phases,
    is ``rejected``; a repairable pre-collection code is ``auto_applied`` only
    when it is on the explicit apply allowlist, otherwise ``proposal_only``.
    """
    spec = SPECS.get(code)
    _require(spec is not None, 'unknown_error_code')
    if not spec.repairable or spec.phase not in PRE_COLLECTION_PHASES:
        return REJECTED
    if code in APPLICABLE_ERROR_CODES:
        return AUTO_APPLIED
    return PROPOSAL_ONLY


def _scenario(value):
    _require(isinstance(value, dict), 'invalid_scenario')
    scenario = {}
    for key, item in value.items():
        _require(isinstance(key, str) and key in SCENARIO_VOCABULARY, 'scenario_key_not_allowed')
        allowed = set(SCENARIO_VOCABULARY[key]) | {UNKNOWN}
        _require(isinstance(item, str) and item in allowed, 'scenario_value_not_allowed')
        scenario[key] = item
    return MappingProxyType(scenario)


def _failure_modes(value):
    _require(isinstance(value, list), 'invalid_failure_modes')
    modes = []
    seen = set()
    for entry in value:
        _require(isinstance(entry, dict) and set(entry) == set(FAILURE_MODE_KEYS), 'invalid_failure_mode')
        code = entry['code']
        spec = SPECS.get(code) if isinstance(code, str) else None
        _require(spec is not None, 'unknown_error_code')
        _require(code not in seen, 'duplicate_failure_mode')
        seen.add(code)
        # category / phase 必须与 registry 一致；声明值不写入，写入的是 registry 值。
        _require(entry['category'] == spec.category.value, 'error_category_mismatch')
        _require(entry['phase'] == spec.phase, 'error_phase_mismatch')
        signal = entry['signal']
        _require(isinstance(signal, str) and len(signal) <= MAX_SIGNAL_LENGTH
                 and SIGNAL_PATTERN.fullmatch(signal) is not None, 'invalid_signal')
        modes.append(FailureMode(code=code, category=spec.category.value, phase=spec.phase, signal=signal))
    return tuple(modes)


def _repairability(value):
    _require(isinstance(value, dict), 'invalid_repairability')
    repairability = {}
    for code, disposition in value.items():
        _require(isinstance(code, str) and code in SPECS, 'unknown_error_code')
        _require(disposition in DISPOSITIONS, 'invalid_repairability')
        # case 只能声明，不能设定策略：声明值必须等于投影结果。
        _require(disposition == project_repairability(code), 'repairability_mismatch')
        repairability[code] = disposition
    return MappingProxyType(repairability)


def validate_case(case):
    """Validate one raw case and return its read-only :class:`KnowledgeCase`.

    Legacy cases (no v2 field) are accepted and reported as schema version 1.
    Any inconsistency raises :class:`KnowledgeError`; nothing is coerced or
    silently dropped.
    """
    _require(isinstance(case, dict), 'invalid_case')
    _require(isinstance(case.get('id'), str) and bool(case['id']), 'invalid_case_id')
    _require(isinstance(case.get('title'), str), 'invalid_case_title')
    terms = case.get('terms')
    _require(isinstance(terms, list) and all(isinstance(term, str) for term in terms), 'invalid_case_terms')
    _require(isinstance(case.get('guidance'), str), 'invalid_case_guidance')
    scenario = _scenario(case['scenario']) if 'scenario' in case else MappingProxyType({})
    modes = _failure_modes(case['failure_modes']) if 'failure_modes' in case else ()
    repairability = _repairability(case['repairability']) if 'repairability' in case else MappingProxyType({})
    is_v2 = any(key in case for key in V2_KEYS)
    return KnowledgeCase(id=case['id'], title=case['title'], terms=tuple(terms), guidance=case['guidance'],
                         case_schema_version=2 if is_v2 else 1, scenario=scenario,
                         failure_modes=modes, repairability=repairability)


def load(cases=None):
    """Load and validate case knowledge; returns an immutable view.

    ``cases`` is an optional corpus override for deterministic tests; by default
    the frozen ``cases.json`` is read. The input is never mutated.
    """
    if cases is None:
        cases = json.loads(Path(__file__).with_name('cases.json').read_bytes())
    _require(isinstance(cases, list), 'invalid_corpus')
    seen = set()
    view = []
    for case in cases:
        validated = validate_case(case)
        _require(validated.id not in seen, 'duplicate_case_id')
        seen.add(validated.id)
        view.append(validated)
    return tuple(view)
