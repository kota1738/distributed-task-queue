from __future__ import annotations

import uuid

from tests.conftest import submit


def test_submit_returns_201_with_location_and_queued_status(client, alice_headers, users):
    response = submit(client, alice_headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["attempts"] == 0
    assert body["max_attempts"] == 3
    assert response.headers["location"] == f"/jobs/{body['id']}"


def test_submit_signals_the_broker(client, alice_headers, users, broker):
    submit(client, alice_headers)
    assert broker.queue_depth() == 1


def test_submit_still_succeeds_when_the_broker_is_down(
    client, alice_headers, users, broker, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(broker, "notify", boom)

    response = submit(client, alice_headers)
    assert response.status_code == 201
    assert response.json()["status"] == "queued"


def test_unknown_job_type_is_rejected(client, alice_headers, users):
    response = submit(client, alice_headers, type="not.a.real.job")
    assert response.status_code == 422
    assert "known types" in response.json()["detail"]


def test_invalid_payload_is_rejected_before_storage(client, alice_headers, users):
    response = submit(
        client, alice_headers, type="math.sum", payload={"numbers": ["not", "numbers"]}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["message"].startswith("invalid payload")


def test_payload_is_normalised_before_storage(client, alice_headers, users):
    response = submit(client, alice_headers, type="math.sum", payload={"numbers": [1, 2]})
    assert response.status_code == 201
    assert response.json()["payload"] == {"numbers": [1.0, 2.0]}


def test_max_attempts_can_be_set_per_job(client, alice_headers, users):
    response = submit(client, alice_headers, max_attempts=7)
    assert response.json()["max_attempts"] == 7


def test_max_attempts_is_validated(client, alice_headers, users):
    response = submit(client, alice_headers, max_attempts=99)
    assert response.status_code == 422


def test_idempotency_key_returns_the_original_job(client, alice_headers, users):
    headers = {**alice_headers, "Idempotency-Key": "order-42"}
    first = client.post(
        "/api/v1/jobs", json={"type": "echo", "payload": {"value": 1}}, headers=headers
    )
    second = client.post(
        "/api/v1/jobs", json={"type": "echo", "payload": {"value": 1}}, headers=headers
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_different_idempotency_keys_create_different_jobs(client, alice_headers, users):
    ids = set()
    for key in ("a", "b"):
        response = client.post(
            "/api/v1/jobs",
            json={"type": "echo", "payload": {}},
            headers={**alice_headers, "Idempotency-Key": key},
        )
        ids.add(response.json()["id"])
    assert len(ids) == 2


def test_get_job_returns_the_job(client, alice_headers, users):
    job_id = submit(client, alice_headers).json()["id"]
    response = client.get(f"/api/v1/jobs/{job_id}", headers=alice_headers)
    assert response.status_code == 200
    assert response.json()["id"] == job_id


def test_another_users_job_is_hidden(client, alice_headers, bob_headers, users):
    job_id = submit(client, alice_headers).json()["id"]
    response = client.get(f"/api/v1/jobs/{job_id}", headers=bob_headers)
    assert response.status_code == 404


def test_unknown_job_id_is_404(client, alice_headers, users):
    response = client.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=alice_headers)
    assert response.status_code == 404


def test_listing_only_returns_your_jobs(client, alice_headers, bob_headers, users):
    submit(client, alice_headers)
    submit(client, alice_headers)
    submit(client, bob_headers)

    alice_jobs = client.get("/api/v1/jobs", headers=alice_headers).json()
    bob_jobs = client.get("/api/v1/jobs", headers=bob_headers).json()

    assert alice_jobs["total"] == 2
    assert bob_jobs["total"] == 1
    assert len(alice_jobs["items"]) == 2


def test_listing_supports_status_filter_and_pagination(client, alice_headers, users):
    for _ in range(3):
        submit(client, alice_headers)

    page = client.get("/api/v1/jobs?limit=2&offset=0", headers=alice_headers).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["limit"] == 2

    queued = client.get("/api/v1/jobs?status=queued", headers=alice_headers).json()
    assert queued["total"] == 3
    failed = client.get("/api/v1/jobs?status=failed", headers=alice_headers).json()
    assert failed["total"] == 0


def test_limit_is_bounded(client, alice_headers, users):
    assert client.get("/api/v1/jobs?limit=1000", headers=alice_headers).status_code == 422


def test_result_is_conflict_until_the_job_succeeds(client, alice_headers, users):
    job_id = submit(client, alice_headers).json()["id"]
    response = client.get(f"/api/v1/jobs/{job_id}/result", headers=alice_headers)
    assert response.status_code == 409
    assert "queued" in response.json()["detail"]


def test_cancel_queued_job(client, alice_headers, users):
    job_id = submit(client, alice_headers).json()["id"]
    response = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=alice_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancelled_job_cannot_be_cancelled_again(client, alice_headers, users):
    job_id = submit(client, alice_headers).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/cancel", headers=alice_headers)
    response = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=alice_headers)
    assert response.status_code == 409


def test_retry_requires_a_failed_or_cancelled_job(client, alice_headers, users):
    job_id = submit(client, alice_headers).json()["id"]
    response = client.post(f"/api/v1/jobs/{job_id}/retry", headers=alice_headers)
    assert response.status_code == 409


def test_retry_resets_a_cancelled_job(client, alice_headers, users):
    job_id = submit(client, alice_headers).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/cancel", headers=alice_headers)

    response = client.post(f"/api/v1/jobs/{job_id}/retry", headers=alice_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["attempts"] == 0
    assert body["finished_at"] is None


def test_admin_sees_every_job(client, alice_headers, bob_headers, admin_headers, users):
    submit(client, alice_headers)
    submit(client, bob_headers)

    response = client.get("/api/v1/admin/jobs", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_non_admin_cannot_list_all_jobs(client, alice_headers, users):
    response = client.get("/api/v1/admin/jobs", headers=alice_headers)
    assert response.status_code == 403


def test_job_type_catalogue_is_public(client, users):
    response = client.get("/api/v1/job-types")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert names == {"echo", "math.sum", "sleep", "flaky"}


def test_health_and_readiness(client, users):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "broker": "ok"}


def test_readiness_reports_a_broken_broker(client, users, broker, monkeypatch):
    monkeypatch.setattr(broker, "ping", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["broker"] == "unavailable"
