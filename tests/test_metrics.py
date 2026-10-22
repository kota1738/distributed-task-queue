"""Tests for the Prometheus endpoint and the counters behind it."""

from __future__ import annotations

import pytest
from prometheus_client.parser import text_string_to_metric_families

from app.worker import Worker
from tests.conftest import submit


@pytest.fixture
def worker(client, settings, broker) -> Worker:
    return Worker(client.app.state.session_factory, broker, settings, worker_id="metrics-worker")


def value_of(client, name: str, **labels: str) -> float:
    """Current value of a sample, or 0 when it has not been created yet.

    The registry is process-wide, so earlier tests in the same run contribute to
    the totals; tests compare before and after rather than absolute values.
    """
    body = client.get("/metrics").text
    total = 0.0
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(key) == want for key, want in labels.items()):
                total += sample.value
    return total


def test_metrics_endpoint_is_scrapeable(client) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "taskqueue_jobs" in response.text


def test_submission_increments_the_counter(client, alice_headers) -> None:
    before = value_of(client, "taskqueue_jobs_submitted_total", type="echo")

    assert submit(client, alice_headers).status_code == 201

    after = value_of(client, "taskqueue_jobs_submitted_total", type="echo")
    assert after == before + 1


def test_queue_depth_is_read_from_the_database(client, alice_headers) -> None:
    before = value_of(client, "taskqueue_jobs", status="queued")
    submit(client, alice_headers)
    submit(client, alice_headers)

    after = value_of(client, "taskqueue_jobs", status="queued")
    assert after == before + 2


def test_successful_job_moves_between_states(client, alice_headers, worker) -> None:
    queued_before = value_of(client, "taskqueue_jobs", status="queued")
    finished_before = value_of(
        client, "taskqueue_jobs_finished_total", type="echo", status="succeeded"
    )

    submit(client, alice_headers)
    assert value_of(client, "taskqueue_jobs", status="queued") == queued_before + 1

    assert worker.run_once() is True

    assert value_of(client, "taskqueue_jobs", status="queued") == queued_before
    finished_after = value_of(
        client, "taskqueue_jobs_finished_total", type="echo", status="succeeded"
    )
    assert finished_after == finished_before + 1


def test_retry_increments_the_retry_counter(client, alice_headers, worker) -> None:
    before = value_of(client, "taskqueue_job_retries_total", type="flaky")
    submit(client, alice_headers, type="flaky", payload={"succeed_on_attempt": 3})

    assert worker.run_once() is True

    assert value_of(client, "taskqueue_job_retries_total", type="flaky") == before + 1
