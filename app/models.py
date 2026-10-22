"""SQLAlchemy models.

All timestamps are stored as naive UTC datetimes. That keeps SQLite (tests,
single-process local runs) and Postgres (production) behaviour identical.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC 'now' used for every stored timestamp."""
    return datetime.utcnow()


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled)


class UserRole(str, enum.Enum):
    user = "user"
    admin = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=16),
        nullable=False,
        default=UserRole.user,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    jobs: Mapped[list[Job]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_jobs_owner_idempotency_key"),
        Index("ix_jobs_claim", "status", "next_run_at"),
        Index("ix_jobs_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=16),
        nullable=False,
        default=JobStatus.queued,
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    owner: Mapped[User] = relationship(back_populates="jobs")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal
