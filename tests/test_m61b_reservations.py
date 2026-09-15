"""M6.1-B1 future-held reservation contract.

A reservation names a capability to cover later and nothing else. These checks
prove that no answer-bearing data can ride along, and that B1 created no dataset
asset: the 28 executable cases and the real reservation list belong to B5.
"""
import copy
from pathlib import Path

import pytest

from backend.app.pipeline.errors import SPECS
from evaluation.schemas.reservations import (CAPABILITY_RESULT_TARGETS,
                                             FORBIDDEN_RESERVATION_KEYS, FUTURE_HELD_SPLIT,
                                             ReservationV1, validate_reservations)

ROOT = Path(__file__).resolve().parents[1]
HELD_FAMILIES = ['total_contract_violation', 'nested_post_composite', 'schema_drift_variant',
                 'empty_edge_shapes', 'alias_multi_level', 'stop_signal_ambiguity']
REGISTRY = ([{'id': name, 'split': FUTURE_HELD_SPLIT} for name in HELD_FAMILIES]
            + [{'id': 'probe_family', 'split': 'development'},
               {'id': 'author_exposed_family', 'split': 'author_exposed_evaluation'}])

# The B design's twelve held slots: six families, two capabilities each.
TWELVE = [
    ('m6r_total_overflow', 'total_contract_violation', 'too_many_items'),
    ('m6r_total_shortfall', 'total_contract_violation', 'incomplete_items'),
    ('m6r_post_composite_total', 'nested_post_composite', 'success_complete'),
    ('m6r_post_composite_flag', 'nested_post_composite', 'success_stop_condition_only'),
    ('m6r_non_finite_number', 'schema_drift_variant', 'non_finite_number'),
    ('m6r_field_type_drift', 'schema_drift_variant', 'field_type_changed'),
    ('m6r_empty_root_list', 'empty_edge_shapes', 'no_list_sample'),
    ('m6r_empty_list_sample', 'empty_edge_shapes', 'empty_list_sample'),
    ('m6r_deep_alias_success', 'alias_multi_level', 'success_complete'),
    ('m6r_deep_alias_rejection', 'alias_multi_level', 'unclassified_rejection'),
    ('m6r_stop_signal_ambiguous', 'stop_signal_ambiguity', 'invalid_has_next'),
    ('m6r_page_budget_held', 'stop_signal_ambiguity', 'partial_boundary'),
]


def reservation(identifier, family, target):
    return {'reservation_id': identifier, 'planned_family': family, 'capability_target': target,
            'intended_split': FUTURE_HELD_SPLIT, 'exposure': 'held_reserved'}


def twelve():
    return [reservation(*row) for row in TWELVE]


def test_valid_reservation_has_exactly_five_fields():
    entry = reservation('m6r_probe', 'total_contract_violation', 'too_many_items')
    model = ReservationV1.model_validate(entry)
    assert set(model.model_dump()) == {'reservation_id', 'planned_family', 'capability_target',
                                       'intended_split', 'exposure'}
    assert len(model.model_dump()) == 5
    assert model.model_dump_json()


def test_valid_twelve_reservation_list():
    validated = validate_reservations(twelve(), REGISTRY)
    assert len(validated) == 12
    assert len({item.reservation_id for item in validated}) == 12
    # Two slots per held family, and every capability target is in the closed set.
    assert sorted(item.planned_family for item in validated).count('schema_drift_variant') == 2
    for item in validated:
        assert item.capability_target in set(SPECS) | CAPABILITY_RESULT_TARGETS


def test_capability_targets_derive_from_the_taxonomy():
    for target in ('too_many_items', 'incomplete_items', 'non_finite_number', 'invalid_has_next',
                   'field_type_changed', 'no_list_sample', 'empty_list_sample'):
        assert target in SPECS
        assert ReservationV1.model_validate(
            reservation('m6r_probe', 'schema_drift_variant', target)).capability_target == target
    for target in sorted(CAPABILITY_RESULT_TARGETS):
        assert target not in SPECS
        ReservationV1.model_validate(reservation('m6r_probe', 'alias_multi_level', target))


@pytest.mark.parametrize('dropped', ['reservation_id', 'planned_family', 'capability_target',
                                     'intended_split', 'exposure'])
def test_missing_any_field_is_rejected(dropped):
    entry = reservation('m6r_probe', 'total_contract_violation', 'too_many_items')
    del entry[dropped]
    with pytest.raises(ValueError):
        ReservationV1.model_validate(entry)
    with pytest.raises(ValueError):
        validate_reservations([entry], REGISTRY)


def test_four_field_reservation_without_exposure_is_rejected():
    entry = reservation('m6r_probe', 'total_contract_violation', 'too_many_items')
    del entry['exposure']
    assert len(entry) == 4
    with pytest.raises(ValueError):
        validate_reservations([entry], REGISTRY)


@pytest.mark.parametrize('extra', ['filled_by_case', 'case_id', 'version', 'note', 'signature'])
def test_sixth_field_is_rejected(extra):
    entry = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'), extra: 'x'}
    assert len(entry) == 6
    with pytest.raises(ValueError):
        validate_reservations([entry], REGISTRY)


@pytest.mark.parametrize('key', sorted(FORBIDDEN_RESERVATION_KEYS))
def test_forbidden_answer_data_is_rejected(key):
    entry = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'), key: 'x'}
    with pytest.raises(ValueError) as caught:
        validate_reservations([entry], REGISTRY)
    assert 'forbidden_answer_data' in str(caught.value) or 'extra_forbidden' in str(caught.value)


@pytest.mark.parametrize('key', ['expected_outcome', 'expected_error_code', 'expected_records',
                                 'expected_anything'])
def test_expected_prefix_family_is_rejected(key):
    entry = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'), key: 'x'}
    with pytest.raises(ValueError):
        validate_reservations([entry], REGISTRY)


@pytest.mark.parametrize('entry,mutation', [
    ('oracle', {'oracle': {'ref': 'evaluation/datasets/m6-v1b/oracles/x.json'}}),
    ('labels', {'retrieval_labels': {'ref': 'evaluation/datasets/m6-v1b/labels/x.json'}}),
    ('plan', {'plan': {'items_pointer': '/records'}}),
    ('expected_outcome', {'expected_outcome': 'success'}),
    ('records', {'records': [{'id': 1}]}),
])
def test_answer_bearing_structures_are_rejected(entry, mutation):
    data = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'), **mutation}
    with pytest.raises(ValueError):
        validate_reservations([data], REGISTRY)


def test_wrong_intended_split_is_rejected():
    for split in ('development', 'author_exposed_evaluation', 'frozen_evaluation', 'held'):
        entry = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'),
                 'intended_split': split}
        with pytest.raises(ValueError):
            ReservationV1.model_validate(entry)


def test_wrong_exposure_is_rejected():
    for exposure in ('author_exposed', 'held', 'public', 'unknown'):
        entry = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'),
                 'exposure': exposure}
        with pytest.raises(ValueError):
            ReservationV1.model_validate(entry)


@pytest.mark.parametrize('target', ['invented_capability', 'success', '', 'SUCCESS_COMPLETE',
                                    'pointer_not_found_applied', 5, None, True])
def test_unknown_capability_target_is_rejected(target):
    entry = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'),
             'capability_target': target}
    with pytest.raises(ValueError):
        validate_reservations([entry], REGISTRY)


@pytest.mark.parametrize('identifier', ['m6_probe', 'probe', 'M6R_probe', 'm6r_', 'm6r_Probe',
                                        '../escape', 'm6r_a' * 30, 5])
def test_invalid_reservation_id_is_rejected(identifier):
    entry = {**reservation('m6r_probe', 'total_contract_violation', 'too_many_items'),
             'reservation_id': identifier}
    with pytest.raises(ValueError):
        validate_reservations([entry], REGISTRY)


def test_duplicate_reservation_id_is_rejected():
    entries = twelve()
    entries[1] = copy.deepcopy(entries[0])
    with pytest.raises(ValueError) as caught:
        validate_reservations(entries, REGISTRY)
    assert 'duplicate_reservation_id' in str(caught.value)


def test_unknown_planned_family_is_rejected():
    entry = reservation('m6r_probe', 'family_that_does_not_exist', 'too_many_items')
    with pytest.raises(ValueError) as caught:
        validate_reservations([entry], REGISTRY)
    assert 'planned_family_not_future_held' in str(caught.value)


@pytest.mark.parametrize('family', ['probe_family', 'author_exposed_family'])
def test_planned_family_on_the_wrong_split_is_rejected(family):
    entry = reservation('m6r_probe', family, 'too_many_items')
    with pytest.raises(ValueError) as caught:
        validate_reservations([entry], REGISTRY)
    assert 'planned_family_not_future_held' in str(caught.value)


def test_family_registry_is_validated():
    entry = reservation('m6r_probe', 'total_contract_violation', 'too_many_items')
    for registry in ('not-a-list', 5, None, [{'id': 'total_contract_violation'}],
                     [{'split': FUTURE_HELD_SPLIT}], [{'id': 'Bad ID', 'split': FUTURE_HELD_SPLIT}],
                     [{'id': 'dup', 'split': FUTURE_HELD_SPLIT},
                      {'id': 'dup', 'split': FUTURE_HELD_SPLIT}]):
        with pytest.raises(ValueError):
            validate_reservations([entry], registry)
    # An empty registry leaves no held family, so every reservation is refused.
    with pytest.raises(ValueError):
        validate_reservations([entry], [])


def test_reservation_list_shape_is_validated():
    for bad in ('not-a-list', 5, None, [5], ['text'], [[{'reservation_id': 'm6r_x'}]]):
        with pytest.raises(ValueError):
            validate_reservations(bad, REGISTRY)


def test_reservations_are_never_cases():
    """A reservation must not be usable as an executable case, and vice versa."""
    from evaluation.schemas.contract_v2 import CaseV2
    from evaluation.schemas.reservations import ReservationV1 as Reservation
    entry = reservation('m6r_probe', 'total_contract_violation', 'too_many_items')
    with pytest.raises(ValueError):
        CaseV2.model_validate(entry)
    # Reservations carry no split, no oracle and no hashes, so they cannot be scored.
    assert 'split' in FORBIDDEN_RESERVATION_KEYS
    assert 'oracle' in FORBIDDEN_RESERVATION_KEYS
    assert 'hashes' in FORBIDDEN_RESERVATION_KEYS
    with pytest.raises(ValueError):
        Reservation.model_validate({**entry, 'split': FUTURE_HELD_SPLIT})


def test_b1_created_no_dataset_or_harness_asset():
    """B1 is schema only: no m6-v1b bundle, no corpora, no harness, no runs."""
    for path in ('evaluation/datasets/m6-v1b', 'evaluation/corpora/m6-v1b',
                 'evaluation/harness', 'evaluation/runs', 'evaluation/reports',
                 'evaluation/fixtures/build_dataset_v2.py', 'evaluation/schemas/overlap.py',
                 'evaluation/schemas/metrics.py'):
        assert not (ROOT / path).exists(), path


def test_m61a_bundle_is_untouched_by_b1():
    from evaluation.schemas.contract import validate_bundle
    from evaluation.schemas.contract_v2 import assert_m61a_frozen
    assert len(validate_bundle(ROOT)) == 12
    assert assert_m61a_frozen(ROOT) == 12
