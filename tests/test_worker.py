from __future__ import annotations

import os
import threading
import uuid
from datetime import timedelta

import pytest

from app import handlers
from app.handlers import JobType
from app.models import Job, JobStatus, utcnow
from app.worker import Worker
from tests.conftest import submit


@pytest.fixture
def worker(client, settings, broker) -> Worker:
    return Worker(client.app.state.session_factory, broker, settings, worker_id="test-worker")


def to_uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def load_job(session_factory, job_id) -> Job:
    with session_factory() as session:
        job = session.get(Job, to_uuid(job_id))
        assert job is not None
        session.refresh(job)
        session.expunge(job)
        return job


def test_worker_processes_a_queued_job(
    client, alice_headers, users, worker, session_factory, broker
):
    job_id = submit(client, alice_headers).json()["id"]

    assert worker.run_once() is True

    job = load_job(session_factory, job_id)
    assert job.status is JobStatus.succeeded
    assert job.result == {"value": "hello"}
    assert job.attempts == 1
    assert job.started_at is not None and job.finished_at is not None
    assert job.error is None
    assert broker.processing_depth() == 0


def test_worker_returns_false_when_there_is_no_work(worker):
    assert worker.run_once() is False


def test_successful_job_is_not_processed_twice(
    client, alice_headers, users, worker, session_factory
):
    job_id = submit(client, alice_headers).json()["id"]
    worker.run_once()
    assert worker.run_once() is False
    assert load_job(session_factory, job_id).attempts == 1


def test_failed_job_is_retried_then_succeeds(client, alice_headers, users, worker, session_factory):
    response = submit(
        client,
        alice_headers,
        type="flaky",
        payload={"succeed_on_attempt": 2},
        max_attempts=3,
    )
    job_id = response.json()["id"]

    worker.run_once()
    first = load_job(session_factory, job_id)
    assert first.status is JobStatus.queued
    assert first.attempts == 1
    assert "simulated failure" in first.error
    assert first.next_run_at > utcnow() - timedelta(seconds=5)

    worker.run_once()
    second = load_job(session_factory, job_id)
    assert second.status is JobStatus.succeeded
    assert second.attempts == 2
    assert second.result == {"succeeded_on_attempt": 2}
    assert second.error is None


def test_job_fails_when_attempts_are_exhausted(
    client, alice_headers, users, worker, session_factory
):
    response = submit(
        client,
        alice_headers,
        type="flaky",
        payload={"succeed_on_attempt": 5},
        max_attempts=2,
    )
    job_id = response.json()["id"]

    worker.run_once()
    worker.run_once()
    job = load_job(session_factory, job_id)

    assert job.status is JobStatus.failed
    assert job.attempts == 2
    assert "simulated failure" in job.error
    assert job.finished_at is not None


def test_unknown_job_type_fails_permanently(client, users, worker, session_factory):
    with session_factory() as session:
        job = Job(
            owner_id=users["alice"].id,
            type="removed.handler",
            payload={},
            max_attempts=3,
            next_run_at=utcnow(),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    worker.run_once()
    stored = load_job(session_factory, job_id)
    assert stored.status is JobStatus.failed
    assert stored.attempts == 1
    assert "permanent failure" in stored.error


def test_unexpected_handler_errors_are_retried_then_fail(
    client, alice_headers, users, worker, session_factory, monkeypatch
):
    def explode(payload, ctx):
        raise RuntimeError("handler bug")

    monkeypatch.setitem(
        handlers.JOB_TYPES,
        "echo",
        JobType(
            name="echo", description="broken", payload_model=handlers.EchoPayload, handler=explode
        ),
    )
    job_id = submit(client, alice_headers, max_attempts=2).json()["id"]

    worker.run_once()
    assert load_job(session_factory, job_id).status is JobStatus.queued
    worker.run_once()

    job = load_job(session_factory, job_id)
    assert job.status is JobStatus.failed
    assert "RuntimeError: handler bug" in job.error


def test_cancelled_job_is_never_executed(client, alice_headers, users, worker, session_factory):
    job_id = submit(client, alice_headers).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/cancel", headers=alice_headers)

    assert worker.run_once() is False
    job = load_job(session_factory, job_id)
    assert job.status is JobStatus.cancelled
    assert job.attempts == 0


def test_late_jobs_are_not_claimed_early(client, alice_headers, users, worker, session_factory):
    job_id = submit(client, alice_headers).json()["id"]
    with session_factory() as session:
        session.get(Job, to_uuid(job_id)).next_run_at = utcnow() + timedelta(minutes=5)
        session.commit()

    assert worker.run_once() is False
    assert load_job(session_factory, job_id).status is JobStatus.queued


def test_stale_running_jobs_are_requeued(
    client, alice_headers, users, worker, session_factory, settings
):
    job_id = submit(client, alice_headers).json()["id"]
    with session_factory() as session:
        job = session.get(Job, to_uuid(job_id))
        job.status = JobStatus.running
        job.started_at = utcnow() - timedelta(seconds=settings.worker_lease_seconds + 60)
        session.commit()

    assert worker.reap_stale_jobs() == 1

    job = load_job(session_factory, job_id)
    assert job.status is JobStatus.queued
    assert "lease expired" in job.error


def test_reaper_leaves_fresh_running_jobs_alone(
    client, alice_headers, users, worker, session_factory
):
    job_id = submit(client, alice_headers).json()["id"]
    with session_factory() as session:
        job = session.get(Job, to_uuid(job_id))
        job.status = JobStatus.running
        job.started_at = utcnow()
        session.commit()

    assert worker.reap_stale_jobs() == 0
    assert load_job(session_factory, job_id).status is JobStatus.running


pytestmark_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="needs Postgres for FOR UPDATE SKIP LOCKED semantics",
)


@pytestmark_postgres
def test_concurrent_workers_claim_distinct_jobs(
    client, alice_headers, users, settings, session_factory
):
    """Four workers racing one queue must never run the same job twice."""
    job_ids = {
        submit(client, alice_headers, payload={"value": index}).json()["id"] for index in range(4)
    }

    for job_id in job_ids:
        client.post(f"/api/v1/jobs/{job_id}/cancel", headers=alice_headers)
        client.post(f"/api/v1/jobs/{job_id}/retry", headers=alice_headers)

    barrier = threading.Barrier(4)
    claimed: list[str] = []

    def run(worker_name: str) -> None:
        worker = Worker(session_factory, _NoopBroker(), settings, worker_id=worker_name)
        barrier.wait(timeout=5)
        worker.run_once()

    threads = [threading.Thread(target=run, args=(f"w{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with session_factory() as session:
        for job_id in job_ids:
            job = session.get(Job, to_uuid(job_id))
            assert job.attempts == 1, f"job {job_id} was claimed {job.attempts} times"
            assert job.status is JobStatus.succeeded
            claimed.append(str(job.id))

    assert len(set(claimed)) == 4


class _NoopBroker:
    """Keeps the race on the database claim rather than the wake-up channel."""

    def reserve(self, timeout_seconds: float) -> str | None:
        return None

    def ack(self, job_id: str) -> None:
        return None

    def notify(self, job_id: str) -> None:
        return None


def test_slow_handler_still_completes(client, alice_headers, users, worker, session_factory):
    response = submit(client, alice_headers, type="sleep", payload={"seconds": 0.05})
    job_id = response.json()["id"]

    worker.run_once()
    job = load_job(session_factory, job_id)
    assert job.status is JobStatus.succeeded
    assert job.result == {"slept_seconds": 0.05}
