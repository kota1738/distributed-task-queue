"""Background worker.

The loop is deliberately small:

1. Block on Redis for a signal (this doubles as the polling interval).
2. Claim the oldest due job in Postgres with ``FOR UPDATE SKIP LOCKED`` so
   multiple workers never run the same job.
3. Execute the handler, then record success, a retry with exponential backoff,
   or a terminal failure.
"""

from __future__ import annotations

import logging
import signal
import threading
import uuid
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app import handlers
from app.broker import Broker
from app.config import Settings
from app.handlers import JobContext, JobExecutionError, PermanentJobError
from app.models import Job, JobStatus, utcnow

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: Broker,
        settings: Settings,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.broker = broker
        self.settings = settings
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._iterations = 0

    # ------------------------------------------------------------------ loop
    def stop(self, *_args: object) -> None:
        logger.info("%s: shutdown requested", self.worker_id)
        self._stop.set()

    def run_forever(self) -> None:
        self.reap_stale_jobs()
        logger.info("%s: started", self.worker_id)
        while not self._stop.is_set():
            try:
                worked = self.run_once()
                self._iterations += 1
                if not worked:
                    self._stop.wait(self.settings.worker_poll_interval_seconds)
                if self._iterations % 60 == 0:
                    self.reap_stale_jobs()
            except Exception:  # keep the worker alive through unexpected errors
                logger.exception("%s: unhandled error in worker loop", self.worker_id)
                self._stop.wait(self.settings.worker_poll_interval_seconds)
        logger.info("%s: stopped", self.worker_id)

    def run_once(self) -> bool:
        """Claim and process at most one job. Returns True if work was done."""
        signal_id = None
        try:
            signal_id = self.broker.reserve(self.settings.worker_poll_interval_seconds)
        except Exception:
            logger.warning("%s: broker unavailable, falling back to DB polling", self.worker_id)

        with self.session_factory() as session:
            job = self._claim(session)

        if job is None:
            if signal_id is not None:
                self._safe_ack(signal_id)
            return False

        try:
            self.process(job)
        finally:
            if signal_id is not None:
                self._safe_ack(signal_id)
        return True

    def _safe_ack(self, job_id: str) -> None:
        try:
            self.broker.ack(job_id)
        except Exception:
            logger.debug("could not ack %s on the broker", job_id, exc_info=True)

    # ----------------------------------------------------------------- claim
    def _claim(self, session: Session) -> Job | None:
        """Move the oldest due job to ``running`` and return it.

        On Postgres the candidate row is locked with ``FOR UPDATE SKIP LOCKED``,
        so simultaneous workers pick different jobs instead of queueing behind
        each other. SQLite has no such clause; the ``status == queued`` guard on
        the UPDATE keeps the claim safe there.
        """
        now = utcnow()
        candidate = (
            select(Job.id)
            .where(Job.status == JobStatus.queued, Job.next_run_at <= now)
            .order_by(Job.next_run_at, Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job_id = session.scalar(candidate)
        if job_id is None:
            session.rollback()
            return None

        claimed = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.queued)
            .values(
                status=JobStatus.running,
                attempts=Job.attempts + 1,
                started_at=now,
                updated_at=now,
            )
            .returning(Job.id)
        ).scalar_one_or_none()
        session.commit()
        if claimed is None:
            return None
        job = session.get(Job, claimed)
        if job is not None:
            logger.info(
                "%s: claimed job %s (%s) attempt %s/%s",
                self.worker_id,
                job.id,
                job.type,
                job.attempts,
                job.max_attempts,
            )
        return job

    # --------------------------------------------------------------- process
    def process(self, job: Job) -> None:
        ctx = JobContext(job_id=job.id, attempt=job.attempts, max_attempts=job.max_attempts)
        try:
            job_type = handlers.get_job_type(job.type)
            result = job_type.handler(dict(job.payload or {}), ctx)
        except PermanentJobError as exc:
            self._finish(job.id, JobStatus.failed, error=f"permanent failure: {exc}")
        except JobExecutionError as exc:
            self._retry_or_fail(job, str(exc))
        except Exception as exc:  # noqa: BLE001 - handler bugs must not kill the worker
            logger.exception("%s: job %s raised", self.worker_id, job.id)
            self._retry_or_fail(job, f"{type(exc).__name__}: {exc}")
        else:
            if not isinstance(result, dict):
                result = {"result": result}
            self._finish(job.id, JobStatus.succeeded, result=result)

    def _retry_or_fail(self, job: Job, error: str) -> None:
        if job.attempts >= job.max_attempts:
            self._finish(job.id, JobStatus.failed, error=error)
            return
        delay = min(
            self.settings.retry_base_delay_seconds * (2 ** (job.attempts - 1)),
            self.settings.retry_max_delay_seconds,
        )
        with self.session_factory() as session:
            session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == JobStatus.running)
                .values(
                    status=JobStatus.queued,
                    error=error,
                    next_run_at=utcnow() + timedelta(seconds=delay),
                    updated_at=utcnow(),
                )
            )
            session.commit()
        logger.info(
            "%s: job %s failed (attempt %s/%s), retrying in %.1fs",
            self.worker_id,
            job.id,
            job.attempts,
            job.max_attempts,
            delay,
        )

    def _finish(
        self,
        job_id: uuid.UUID,
        status: JobStatus,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        now = utcnow()
        with self.session_factory() as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.running)
                .values(
                    status=status,
                    result=result,
                    error=error,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        logger.info("%s: job %s -> %s", self.worker_id, job_id, status.value)

    # ------------------------------------------------------- crash recovery
    def reap_stale_jobs(self) -> int:
        """Return jobs whose worker lease expired back to the queue."""
        cutoff = utcnow() - timedelta(seconds=self.settings.worker_lease_seconds)
        with self.session_factory() as session:
            stale = session.scalars(
                select(Job).where(
                    Job.status == JobStatus.running,
                    or_(Job.started_at.is_(None), Job.started_at < cutoff),
                )
            ).all()
            for job in stale:
                session.execute(
                    update(Job)
                    .where(Job.id == job.id, Job.status == JobStatus.running)
                    .values(
                        status=JobStatus.queued,
                        error="worker lease expired; job requeued",
                        next_run_at=utcnow(),
                        updated_at=utcnow(),
                    )
                )
            session.commit()
        if stale:
            logger.warning("%s: requeued %s job(s) with expired leases", self.worker_id, len(stale))
        return len(stale)


def main() -> None:
    from app.config import get_settings
    from app.db import create_db_engine, create_session_factory, init_models

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    settings.validate_for_runtime()

    engine = create_db_engine(settings.database_url)
    init_models(engine)
    factory = create_session_factory(engine)
    broker = Broker(settings.redis_url, settings.queue_name, settings.processing_queue_name)

    worker = Worker(factory, broker, settings)
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
