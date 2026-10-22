"""Redis broker.

Redis is a *wake-up channel*, not the source of truth: Postgres decides which job
a worker runs, so a lost or duplicated signal is harmless. Signals are moved to a
processing list while being handled (``BRPOPLPUSH``), so nothing is dropped if a
worker dies mid-job; the worker's lease reaper recovers those rows.
"""

from __future__ import annotations

from redis import Redis


class Broker:
    def __init__(
        self,
        url: str | None = None,
        queue_name: str = "taskqueue:jobs",
        processing_queue_name: str = "taskqueue:jobs:processing",
        *,
        client: Redis | None = None,
    ) -> None:
        self.queue_name = queue_name
        self.processing_queue_name = processing_queue_name
        if client is not None:
            self._client = client
        elif url is not None:
            self._client = Redis.from_url(url, decode_responses=True)
        else:
            raise ValueError("either url or client must be provided")

    def ping(self) -> bool:
        return bool(self._client.ping())

    def notify(self, job_id: str) -> None:
        """Signal that at least one job may be waiting."""
        self._client.lpush(self.queue_name, job_id)

    def reserve(self, timeout_seconds: float) -> str | None:
        """Take one signal, blocking up to ``timeout_seconds``.

        Redis treats a timeout of 0 as "block forever", so short timeouts fall
        back to a non-blocking pop to keep the worker loop interruptible.
        """
        if timeout_seconds < 1:
            return self._client.rpoplpush(self.queue_name, self.processing_queue_name)
        return self._client.brpoplpush(
            self.queue_name,
            self.processing_queue_name,
            timeout=int(round(timeout_seconds)),
        )

    def ack(self, job_id: str) -> None:
        self._client.lrem(self.processing_queue_name, 1, job_id)

    def queue_depth(self) -> int:
        return int(self._client.llen(self.queue_name))

    def processing_depth(self) -> int:
        return int(self._client.llen(self.processing_queue_name))

    def close(self) -> None:
        self._client.close()
