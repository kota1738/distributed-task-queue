from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, ValidationError

from app import handlers
from app.handlers import (
    JobContext,
    JobExecutionError,
    PermanentJobError,
    get_job_type,
    job_type,
)

CTX = JobContext(job_id=uuid.uuid4(), attempt=1, max_attempts=3)


def run(name: str, payload: dict, *, attempt: int = 1) -> dict:
    jt = get_job_type(name)
    validated = jt.payload_model.model_validate(payload).model_dump()
    return jt.handler(validated, JobContext(CTX.job_id, attempt, 3))


def test_expected_job_types_are_registered():
    assert {"echo", "math.sum", "sleep", "flaky"} <= set(handlers.JOB_TYPES)


def test_echo_returns_the_submitted_value():
    assert run("echo", {"value": {"nested": [1, 2, 3]}}) == {"value": {"nested": [1, 2, 3]}}


def test_math_sum_adds_numbers():
    assert run("math.sum", {"numbers": [1, 2.5, 3]}) == {"sum": 6.5, "count": 3}


def test_math_sum_rejects_an_empty_list():
    with pytest.raises(ValidationError):
        get_job_type("math.sum").payload_model.model_validate({"numbers": []})


def test_sleep_rejects_durations_above_the_cap():
    with pytest.raises(ValidationError):
        get_job_type("sleep").payload_model.model_validate({"seconds": 30})


def test_flaky_fails_before_its_target_attempt():
    with pytest.raises(JobExecutionError):
        run("flaky", {"succeed_on_attempt": 3}, attempt=1)


def test_flaky_succeeds_on_its_target_attempt():
    assert run("flaky", {"succeed_on_attempt": 2}, attempt=2) == {"succeeded_on_attempt": 2}


def test_unknown_job_type_raises_a_permanent_error():
    with pytest.raises(PermanentJobError, match="unknown job type"):
        get_job_type("does.not.exist")


def test_registering_the_same_name_twice_is_rejected():
    class Payload(BaseModel):
        pass

    with pytest.raises(ValueError, match="already registered"):

        @job_type("echo", Payload, "duplicate")
        def _duplicate(payload, ctx):
            return {}


def test_every_job_type_exposes_a_json_schema():
    for jt in handlers.JOB_TYPES.values():
        schema = jt.payload_model.model_json_schema()
        assert isinstance(schema, dict) and schema
