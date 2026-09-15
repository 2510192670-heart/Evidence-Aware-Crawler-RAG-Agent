"""M6.1-B1 future-held reservation contract.

A reservation is a promise to cover a capability later, not a case. It carries no
input, no oracle, no label and no expected answer, so nothing about a held case is
knowable from it and the held data cannot be exposed before M6.1-C fills it.

B1 defines and validates the shape only. No reservation asset is created, and no
fulfillment logic exists here on purpose: recording which case satisfied a
reservation is M6.1-C work and must be done with a separate mapping (or a new
reservation schema version) rather than by editing these immutable entries.

``capability_target`` is closed over ``errors.SPECS`` plus a small set of result
categories, so the vocabulary is derived from the taxonomy authority instead of
duplicating it.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.pipeline.errors import SPECS

__all__ = ['CAPABILITY_RESULT_TARGETS', 'FAMILY_ID', 'FORBIDDEN_RESERVATION_KEYS',
           'FUTURE_HELD_SPLIT', 'RESERVATION_ID', 'ReservationV1', 'validate_reservations']

RESERVATION_ID = r'^m6r_[a-z][a-z0-9_]{1,63}$'
FAMILY_ID = r'^[a-z][a-z0-9_]{1,63}$'
FUTURE_HELD_SPLIT = 'future_held_evaluation'

# Result categories that are not error codes, so a reservation can name a target
# behaviour without naming an answer.
CAPABILITY_RESULT_TARGETS = frozenset({
    'success_complete', 'success_stop_condition_only', 'partial_boundary',
    'proposal_only', 'budget_exhausted', 'unclassified_rejection'})

# Answer-bearing keys. ``extra='forbid'`` already rejects them; naming them keeps
# the refusal explicit and testable, and catches the whole ``expected_*`` family.
FORBIDDEN_RESERVATION_KEYS = frozenset({
    'input', 'inputs', 'fixture', 'oracle', 'oracles', 'labels', 'label',
    'retrieval_labels', 'request', 'plan', 'records', 'record_count', 'page_count',
    'result_sha256', 'relevant_case_ids', 'failure_relevant_case_ids', 'hashes',
    'failure_expectation', 'repair_expectation', 'split', 'expected_outcome',
    'expected_error_code', 'filled_by_case', 'case_id'})


class ReservationV1(BaseModel):
    """Exactly five closed fields; immutable once an m6-v1b bundle is frozen."""

    model_config = ConfigDict(extra='forbid', strict=True)

    reservation_id: str = Field(pattern=RESERVATION_ID)
    planned_family: str = Field(pattern=FAMILY_ID)
    capability_target: str
    intended_split: Literal[FUTURE_HELD_SPLIT]
    exposure: Literal['held_reserved']

    @field_validator('capability_target')
    @classmethod
    def closed_capability(cls, value):
        if value not in SPECS and value not in CAPABILITY_RESULT_TARGETS:
            raise ValueError('unknown_capability_target')
        return value


def _held_family_ids(families):
    if not isinstance(families, (list, tuple)):
        raise ValueError('invalid_family_registry')
    held, seen = set(), set()
    for family in families:
        if not isinstance(family, dict) or 'id' not in family or 'split' not in family:
            raise ValueError('invalid_family_entry')
        identifier, split = family['id'], family['split']
        if not isinstance(identifier, str) or not re.fullmatch(FAMILY_ID, identifier) \
                or identifier in seen or not isinstance(split, str):
            raise ValueError('invalid_family_entry')
        seen.add(identifier)
        if split == FUTURE_HELD_SPLIT:
            held.add(identifier)
    return held


def validate_reservations(reservations, families):
    """Validate reservation metadata against the family registry.

    A reservation may only point at a family whose intended split is future-held,
    which is what keeps a held slot from being attached to a development or
    author-exposed family. Reservations are deliberately absent from scoring,
    retrieval evaluation and every executable case denominator.
    """
    held = _held_family_ids(families)
    if not isinstance(reservations, (list, tuple)):
        raise ValueError('invalid_reservation_list')
    validated, seen = [], set()
    for entry in reservations:
        if not isinstance(entry, dict):
            raise ValueError('invalid_reservation_entry')
        if FORBIDDEN_RESERVATION_KEYS & set(entry) \
                or any(key.startswith('expected_') for key in entry):
            raise ValueError('forbidden_answer_data')
        reservation = ReservationV1.model_validate(entry)
        if reservation.reservation_id in seen:
            raise ValueError('duplicate_reservation_id')
        seen.add(reservation.reservation_id)
        # Covers both an unknown family and a family on the wrong split.
        if reservation.planned_family not in held:
            raise ValueError('planned_family_not_future_held')
        validated.append(reservation)
    return validated
