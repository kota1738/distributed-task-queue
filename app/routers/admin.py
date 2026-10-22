"""Administrator endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.deps import SessionDep
from app.models import Job, JobStatus
from app.schemas import JobListResponse, JobResponse
from app.security import AdminUser

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/jobs", response_model=JobListResponse)
def list_all_jobs(
    session: SessionDep,
    admin: AdminUser,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    """Every user's jobs, newest first. Administrator role required."""
    conditions = []
    if status_filter is not None:
        conditions.append(Job.status == status_filter)

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
