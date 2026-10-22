"""Prometheus metrics for the API and the workers.

The app and the workers run as separate processes, so the counters here are
per-process. Queue depth is the exception: it is read from the database at
scrape time, which is the only place that knows the true state.
"""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job

REGISTRY = CollectorRegistry(auto_describe=True)

JOBS_SUBMITTED = Counter(
    "taskqueue_jobs_submitted_total",
    "Jobs accepted by the API, by job type.",
    ["type"],
    registry=REGISTRY,
)
JOBS_CLAIMED = Counter(
    "taskqueue_jobs_claimed_total",
    "Jobs claimed by a worker, by job type.",
    ["type"],
    registry=REGISTRY,
)
JOBS_FINISHED = Counter(
    "taskqueue_jobs_finished_total",
    "Jobs that reached a terminal state, by type and status.",
    ["type", "status"],
    registry=REGISTRY,
)
JOB_RETRIES = Counter(
    "taskqueue_job_retries_total",
    "Transient failures that scheduled a retry, by job type.",
    ["type"],
    registry=REGISTRY,
)
JOB_DURATION = Counter(
    "taskqueue_job_duration_seconds_total",
    "Total handler execution time, by job type.",
    ["type"],
    registry=REGISTRY,
)
JOB_ATTEMPTS = Counter(
    "taskqueue_job_attempts_total",
    "Handler attempts, including retries, by job type.",
    ["type"],
    registry=REGISTRY,
)

_session_factory: sessionmaker[Session] | None = None


def bind_session_factory(factory: sessionmaker[Session] | None) -> None:
    """Point the queue-depth collector at the application's database."""
    global _session_factory
    _session_factory = factory


class _JobStatusCollector:
    """Reads current job counts from the database on each scrape."""

    def collect(self) -> Any:
        if _session_factory is None:
            return
        try:
            with _session_factory() as session:
                rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        except Exception:  # noqa: BLE001 - a scrape must never crash the API
            return
        gauge = GaugeMetricFamily(
            "taskqueue_jobs", "Jobs currently in each state.", labels=["status"]
        )
        for status_value, count in rows:
            value = getattr(status_value, "value", status_value)
            gauge.add_metric([str(value)], float(count))
        yield gauge


REGISTRY.register(_JobStatusCollector())


def record_submitted(job_type: str) -> None:
    JOBS_SUBMITTED.labels(job_type).inc()


def record_claim(job_type: str, attempt: int) -> None:
    JOBS_CLAIMED.labels(job_type).inc()
    JOB_ATTEMPTS.labels(job_type).inc()


def record_retry(job_type: str) -> None:
    JOB_RETRIES.labels(job_type).inc()


def record_finished(job_type: str, status: str, started_at: float | None = None) -> None:
    JOBS_FINISHED.labels(job_type, status).inc()
    if started_at is not None:
        JOB_DURATION.labels(job_type).inc(max(time.monotonic() - started_at, 0.0))


def render() -> bytes:
    return generate_latest(REGISTRY)
