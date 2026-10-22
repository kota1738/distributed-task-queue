"""Job type registry.

Each job type owns a pydantic payload model (validated at submission time, so a
bad job is rejected with 422 instead of failing later in a worker) and a handler
that returns a JSON-serialisable result.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class JobExecutionError(Exception):
    """A transient failure. The job is retried until ``max_attempts`` is reached."""


class PermanentJobError(Exception):
    """A failure that retrying cannot fix, e.g. a payload the handler rejects."""


@dataclass(frozen=True)
class JobContext:
    job_id: uuid.UUID
    attempt: int
    max_attempts: int


Handler = Callable[[dict[str, Any], JobContext], dict[str, Any]]


@dataclass(frozen=True)
class JobType:
    name: str
    description: str
    payload_model: type[BaseModel]
    handler: Handler


JOB_TYPES: dict[str, JobType] = {}


def job_type(name: str, payload_model: type[BaseModel], description: str) -> Callable:
    """Register a handler under ``name``."""

    def decorator(handler: Handler) -> Handler:
        if name in JOB_TYPES:
            raise ValueError(f"job type {name!r} is already registered")
        JOB_TYPES[name] = JobType(
            name=name, description=description, payload_model=payload_model, handler=handler
        )
        return handler

    return decorator


def get_job_type(name: str) -> JobType:
    try:
        return JOB_TYPES[name]
    except KeyError as exc:
        raise PermanentJobError(f"unknown job type: {name}") from exc


class EchoPayload(BaseModel):
    value: Any = Field(default=None, description="Any JSON value, returned unchanged.")


@job_type("echo", EchoPayload, "Return the submitted value unchanged.")
def _echo(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    return {"value": payload.get("value")}


class SumPayload(BaseModel):
    numbers: list[float] = Field(min_length=1, max_length=10_000)


@job_type("math.sum", SumPayload, "Sum a list of numbers.")
def _sum(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    numbers = payload["numbers"]
    return {"sum": sum(numbers), "count": len(numbers)}


class SleepPayload(BaseModel):
    seconds: float = Field(gt=0, le=5, description="Capped at 5 seconds for demos.")


@job_type("sleep", SleepPayload, "Sleep for a number of seconds, then report it.")
def _sleep(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    seconds = float(payload["seconds"])
    time.sleep(seconds)
    return {"slept_seconds": seconds}


class FlakyPayload(BaseModel):
    succeed_on_attempt: int = Field(ge=1, le=10)


@job_type("flaky", FlakyPayload, "Fail until the given attempt number, then succeed.")
def _flaky(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    target = payload["succeed_on_attempt"]
    if ctx.attempt < target:
        raise JobExecutionError(
            f"simulated failure on attempt {ctx.attempt} (succeeds on {target})"
        )
    return {"succeeded_on_attempt": ctx.attempt}
