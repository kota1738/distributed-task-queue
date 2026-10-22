"""Request and response models for the HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import JobStatus, UserRole


class TokenRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    role: UserRole
    created_at: datetime


class JobCreateRequest(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    status: JobStatus
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_run_at: datetime


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int
    limit: int
    offset: int


class JobTypeResponse(BaseModel):
    name: str
    description: str
    payload_schema: dict[str, Any]


class ErrorResponse(BaseModel):
    detail: str
