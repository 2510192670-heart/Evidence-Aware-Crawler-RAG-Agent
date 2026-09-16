"""User requirements are bounded data, never execution instructions."""
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import SENSITIVE, ExtractionPlan


class RequirementError(ValueError):
    """Stable user-facing code; existing taxonomy fails closed, without repair."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def safe_description(value):
    if SENSITIVE.search(value):
        raise ValueError('sensitive_requirements_not_supported')
    return value.strip()


class RequirementPlan(ExtractionPlan):
    unsupported_requirements: list[str] = Field(max_length=10, description=(
        'Explicitly list requirements the plan cannot execute (filters, aggregation, summaries, '
        'authentication or extra interactions). Return [] only when all requirements are covered.'))


def checked_requirement_plan(plan):
    if plan.unsupported_requirements:
        raise RequirementError('unsupported_requirements')
    # Keep the established execution/artifact plan contract unchanged.
    return ExtractionPlan(**plan.model_dump(exclude={'unsupported_requirements'}))


class FieldSpec(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=200)
    type: Literal['string', 'integer', 'number', 'boolean', 'scalar'] = 'scalar'
    required: bool = True

    @field_validator('name')
    @classmethod
    def check_name(cls, value):
        if not re.fullmatch(r'\w{1,40}', value) or SENSITIVE.search(value):
            raise ValueError('invalid_fields')
        return value

    @field_validator('description')
    @classmethod
    def check_description(cls, value):
        return safe_description(value)


def verify_fields(rows, specs):
    """Validate extracted values without coercion or model-supplied defaults."""
    types = {'string': {str}, 'integer': {int}, 'number': {int, float},
             'boolean': {bool}, 'scalar': {str, int, float, bool}}
    missing = {spec.name: 0 for spec in specs}
    output = []
    for row in rows:
        result = {}
        for spec in specs:
            value = row.get(spec.name)
            if value is None:
                if spec.required:
                    raise RequirementError('required_field_missing')
                missing[spec.name] += 1
            elif type(value) not in types[spec.type] or (type(value) is float and not math.isfinite(value)):
                raise RequirementError('requested_field_type_mismatch')
            result[spec.name] = value
        output.append(result)
    return output, missing
