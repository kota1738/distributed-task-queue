"""Job endpoints: submit, inspect, cancel and retry."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import handlers, metrics
from app.broker import Broker
from app.deps import BrokerDep, CurrentUser, SessionDep, SettingsDep
from app.handlers import PermanentJobError
from app.models import Job, JobStatus, User, UserRole, utcnow
from app.schemas import (
    JobCreateRequest,
    JobListResponse,
    JobResponse,
    JobTypeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def validate_job_request(body: JobCreateRequest) -> tuple[handlers.JobType, dict]:
    """Resolve the job type and validate the payload before it is stored."""
    try:
        job_type = handlers.get_job_type(body.type)
    except PermanentJobError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc}. known types: {', '.join(sorted(handlers.JOB_TYPES))}",
        ) from exc
    try:
        payload = job_type.payload_model.model_validate(body.payload).model_dump()
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": f"invalid payload for job type {body.type!r}",
                "errors": exc.errors(include_url=False),
            },
        ) from exc
    return job_type, payload


def get_owned_job(session: Session, job_id: uuid.UUID, user: User) -> Job:
    """Fetch a job, hiding other users' jobs behind a 404."""
    job = session.get(Job, job_id)
    if job is None or (job.owner_id != user.id and user.role is not UserRole.admin):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


def notify_safely(broker: Broker, job_id: uuid.UUID) -> None:
    """Wake a worker, but never fail the request if Redis is unavailable.

    The job is already durable in Postgres; idle workers poll for due jobs, so a
    missed signal only adds latency.
    """
    try:
        broker.notify(str(job_id))
    except Exception:  # noqa: BLE001
        logger.warning("could not notify broker for job %s", job_id, exc_info=True)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=JobResponse)
def submit_job(
    body: JobCreateRequest,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    broker: BrokerDep,
    user: CurrentUser,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Job:
    """Submit a job for asynchronous processing.

    Repeating a request with the same ``Idempotency-Key`` returns the original
    job with 200 instead of creating a duplicate.
    """
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()[:128] or None

    if idempotency_key:
        existing = session.scalar(
            select(Job).where(Job.owner_id == user.id, Job.idempotency_key == idempotency_key)
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            response.headers["Location"] = f"/jobs/{existing.id}"
            return existing

    _, payload = validate_job_request(body)

    job = Job(
        owner_id=user.id,
        type=body.type,
        payload=payload,
        status=JobStatus.queued,
        max_attempts=body.max_attempts or settings.default_max_attempts,
        idempotency_key=idempotency_key,
        next_run_at=utcnow(),
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    response.headers["Location"] = f"/jobs/{job.id}"
    metrics.record_submitted(job.type)
    notify_safely(broker, job.id)
    return job


@router.get("", response_model=JobListResponse)
def list_jobs(
    session: SessionDep,
    user: CurrentUser,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    job_type: Annotated[str | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    """List your jobs, newest first."""
    conditions = [Job.owner_id == user.id]
    if status_filter is not None:
        conditions.append(Job.status == status_filter)
    if job_type is not None:
        conditions.append(Job.type == job_type)

    total = session.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    items = session.scalars(
        select(Job).where(*conditions).order_by(Job.created_at.desc()).limit(limit).offset(offset)
    ).all()

    return JobListResponse(
        items=[JobResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Job:
    return get_owned_job(session, job_id, user)


@router.get("/{job_id}/result", response_model=JobResponse)
def get_job_result(job_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Job:
    """Return the job only once it has a result, otherwise 409 Conflict."""
    job = get_owned_job(session, job_id, user)
    if job.status is not JobStatus.succeeded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is {job.status.value}; no result available yet",
        )
    return job


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(job_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Job:
    """Cancel a job that has not started. Running jobs cannot be cancelled."""
    job = get_owned_job(session, job_id, user)
    if job.status is not JobStatus.queued:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only queued jobs can be cancelled (job is {job.status.value})",
        )
    job.status = JobStatus.cancelled
    job.finished_at = utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.post("/{job_id}/retry", response_model=JobResponse)
def retry_job(job_id: uuid.UUID, session: SessionDep, user: CurrentUser, broker: BrokerDep) -> Job:
    """Put a failed or cancelled job back on the queue with a fresh attempt count."""
    job = get_owned_job(session, job_id, user)
    if job.status not in (JobStatus.failed, JobStatus.cancelled):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only failed or cancelled jobs can be retried (job is {job.status.value})",
        )
    job.status = JobStatus.queued
    job.attempts = 0
    job.result = None
    job.error = None
    job.started_at = None
    job.finished_at = None
    job.next_run_at = utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    notify_safely(broker, job.id)
    return job


def job_type_catalogue() -> list[JobTypeResponse]:
    """Describe every registered job type and its payload schema."""
    return [
        JobTypeResponse(
            name=jt.name,
            description=jt.description,
            payload_schema=jt.payload_model.model_json_schema(),
        )
        for jt in sorted(handlers.JOB_TYPES.values(), key=lambda j: j.name)
    ]
