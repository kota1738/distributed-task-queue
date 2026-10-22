"""End-to-end walkthrough with no external services.

Runs the real FastAPI app, the real worker and a real SQLite file; only Redis is
replaced by ``fakeredis``. Nothing here is mocked out in the application code.

    python scripts/demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = Path(tempfile.mkdtemp(prefix="taskqueue-demo-"))
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{(TMP / 'demo.db').as_posix()}"
os.environ["JWT_SECRET"] = "demo-secret-long-enough-to-pass-validation-checks"
os.environ["ENVIRONMENT"] = "dev"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
# Retry immediately so the walkthrough finishes in one pass instead of waiting
# out the real exponential backoff (default 1s, 2s, 4s ...).
os.environ["RETRY_BASE_DELAY_SECONDS"] = "0"

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import cli  # noqa: E402
from app.broker import Broker  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker import Worker  # noqa: E402

EMAIL = "demo@example.com"
PASSWORD = "demo-password"


def rule(title: str) -> None:
    print(f"\n{title}\n" + "=" * len(title))


def show_job(label: str, body: dict) -> None:
    print(
        f"  {label:<22} {body['status']:<9} attempts={body['attempts']}/{body['max_attempts']}"
        f"  result={body.get('result') or body.get('error')}"
    )


def main() -> int:
    settings = get_settings()

    rule("1. Create the schema and a user with the admin CLI")
    cli.main(["init-db"])
    cli.main(["create-user", "--email", EMAIL, "--password", PASSWORD, "--admin"])

    broker = Broker(client=fakeredis.FakeRedis(decode_responses=True))
    app = create_app(settings, broker=broker)

    with TestClient(app) as client:
        rule("2. Log in and get a bearer token")
        response = client.post("/api/v1/auth/token", json={"email": EMAIL, "password": PASSWORD})
        response.raise_for_status()
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        print(f"  token acquired (expires in {response.json()['expires_in']}s)")

        rule("3. Discover the job types this deployment can run")
        for job_type in client.get("/api/v1/job-types").json():
            print(f"  {job_type['name']:<10} {job_type['description']}")

        rule("4. Submit work over REST")
        submitted = {}
        for name, payload in [
            ("math.sum", {"numbers": [1, 2, 3, 4.5]}),
            ("flaky", {"succeed_on_attempt": 3}),
            ("echo", {"value": "hello"}),
        ]:
            response = client.post(
                "/api/v1/jobs", json={"type": name, "payload": payload}, headers=headers
            )
            submitted[name] = response.json()
            print(f"  {name:<10} -> 201 Created, job {submitted[name]['id'][:8]},"
                  f" status={submitted[name]['status']}")

        rule("5. Validation happens at submission, not in the worker")
        bad = client.post(
            "/api/v1/jobs", json={"type": "math.sum", "payload": {"numbers": []}}, headers=headers
        )
        print(f"  empty numbers list -> {bad.status_code} ({bad.json()['detail']})")

        rule("6. A repeated Idempotency-Key returns the original job")
        first = client.post(
            "/api/v1/jobs",
            json={"type": "echo", "payload": {"value": "once"}},
            headers={**headers, "Idempotency-Key": "demo-key-1"},
        ).json()
        again = client.post(
            "/api/v1/jobs",
            json={"type": "echo", "payload": {"value": "once"}},
            headers={**headers, "Idempotency-Key": "demo-key-1"},
        ).json()
        print(f"  same job id returned: {first['id'] == again['id']}")

        rule("7. Run a worker until the queue is empty")
        worker = Worker(app.state.session_factory, broker, settings)
        processed = 0
        while worker.run_once():
            processed += 1
        print(f"  worker processed {processed} jobs (the flaky job retried twice)")

        rule("8. Every job settled to a terminal state")
        for name, job in submitted.items():
            show_job(name, client.get(f"/api/v1/jobs/{job['id']}", headers=headers).json())

        rule("9. Admin view across all users")
        all_jobs = client.get("/api/v1/admin/jobs", headers=headers).json()
        print(f"  total jobs in the system: {all_jobs['total']}")

    print(f"\nDemo database: {TMP / 'demo.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
