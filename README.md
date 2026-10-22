# Distributed Task Queue API

An asynchronous job processing service: clients submit work over HTTP, background
workers pick it up, and job status and results are queryable until they are
cleaned up. Built to explore the parts of backend engineering that tutorial
projects usually skip — atomic claiming, retries with backoff, idempotent
submission and crash recovery.

**Stack:** Python 3.12+, FastAPI, SQLAlchemy 2, PostgreSQL, Redis, Docker Compose,
pytest, GitHub Actions.

## What it does

- Submit a job and get an id back immediately (`202`-style asynchronous flow with
  `201 Created` and a `Location` header).
- Four built-in job types for exercising the pipeline: `echo`, `math.sum`,
  `sleep`, `flaky`.
- Track status (`queued`, `running`, `succeeded`, `failed`, `cancelled`), read
  results, cancel queued work, retry failed work.
- Failures retry automatically with exponential backoff until `max_attempts`.
- Duplicate submissions with the same `Idempotency-Key` return the original job.
- JWT bearer authentication, per-owner isolation, and an administrator role.
- Health (`/healthz`) and readiness (`/readyz`) probes for orchestration.

## Architecture

```
              ┌──────────────────────────┐
   client ───►│  FastAPI (app.main)      │
              │  auth · jobs · admin     │
              └───────┬──────────┬───────┘
                      │          │
        writes/reads  │          │ signal (LPUSH)
                      ▼          ▼
              ┌────────────┐  ┌────────────┐
              │ PostgreSQL │  │   Redis    │
              │  users     │  │ wake-up    │
              │  jobs      │  │ channel    │
              └─────▲──────┘  └─────┬──────┘
                    │               │ BRPOPLPUSH
                    │ claim + write │
              ┌─────┴───────────────▼──────┐
              │  Worker (app.worker)       │
              │  handler registry          │
              └────────────────────────────┘
```

Redis is a **wake-up channel, not the source of truth**. Job state lives in
Postgres, so a lost or duplicated signal cannot lose work: an idle worker polls
the database for due jobs anyway.

See [`docs/architecture.md`](docs/architecture.md) for the request lifecycle,
data model and failure handling.

## Quick start (Docker Compose)

```bash
cp .env.example .env          # then set JWT_SECRET
docker compose up --build
```

The API starts on <http://localhost:8000> (interactive docs at `/docs`), with
Postgres, Redis and two worker replicas. Create a user, then call the API:

```bash
docker compose exec api python -m app.cli create-user --email me@example.com --password 'change-me-123'
```

## Quick start (local, no Docker)

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env                 # point DATABASE_URL / REDIS_URL at your services
python -m app.cli init-db
python -m app.cli create-user --email me@example.com --password 'change-me-123'

uvicorn app.main:app --reload        # terminal 1: API
python -m app.worker                 # terminal 2: worker
```

## Walking through the API

To watch the whole lifecycle without installing anything, run the demo — it
boots the real app, worker and database, and prints each step:

```bash
python scripts/demo.py
```

Or drive it by hand:

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"change-me-123"}' | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# Submit work
curl -s -X POST localhost:8000/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"math.sum","payload":{"numbers":[1,2,3.5]}}'

# Poll it, then read the result once it has succeeded
curl -s localhost:8000/api/v1/jobs/<job-id>        -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/api/v1/jobs/<job-id>/result -H "Authorization: Bearer $TOKEN"
```

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/auth/token` | Exchange email + password for a bearer token |
| `GET` | `/api/v1/auth/me` | Current user |
| `GET` | `/api/v1/job-types` | Supported job types and their JSON schemas |
| `POST` | `/api/v1/jobs` | Submit a job (supports `Idempotency-Key`) |
| `GET` | `/api/v1/jobs` | List your jobs (`status`, `type`, `limit`, `offset`) |
| `GET` | `/api/v1/jobs/{id}` | Job status |
| `GET` | `/api/v1/jobs/{id}/result` | Result, `409` until the job succeeds |
| `POST` | `/api/v1/jobs/{id}/cancel` | Cancel a queued job |
| `POST` | `/api/v1/jobs/{id}/retry` | Requeue a failed or cancelled job |
| `GET` | `/api/v1/admin/jobs` | All jobs (administrator role) |
| `GET` | `/healthz`, `/readyz` | Liveness, readiness |

Payloads are validated against the job type's schema **before** the job is
stored, so a bad request fails fast with `422` instead of failing later in a
worker.

## Testing

```bash
pytest                        # SQLite + in-process fake Redis, no services needed
ruff check . && ruff format --check .
```

The suite runs the same 64 tests in two configurations:

| Configuration | Database | Redis |
| --- | --- | --- |
| Local default | SQLite file per test | `fakeredis` in-process |
| CI / `TEST_DATABASE_URL` | PostgreSQL | Redis 7 |

To run against real services:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/taskqueue_test \
TEST_REDIS_URL=redis://localhost:6379/0 \
pytest
```

What is covered: authentication and token expiry, ownership isolation, payload
validation, idempotency, pagination, cancel/retry rules, successful processing,
retry-then-succeed, retry exhaustion, permanent failures, unknown job types,
lease reaping, readiness degradation, and (on Postgres) four workers racing one
queue without double-claiming.

## Design decisions

**Atomic claiming.** Workers select the oldest due job with
`FOR UPDATE SKIP LOCKED`, then flip it to `running` with a `status = 'queued'`
guard. Concurrent workers take different rows instead of blocking or
double-processing, and the guard keeps the claim correct on databases without
`SKIP LOCKED`.

**Retries with backoff.** Attempts are incremented at claim time, so a worker
crash still counts as an attempt. Transient failures set `next_run_at` to
`now + base * 2^(attempt-1)` capped at `RETRY_MAX_DELAY_SECONDS`; permanent
failures (for example an unregistered job type) fail immediately instead of
burning attempts.

**Crash recovery.** Jobs stuck in `running` past `WORKER_LEASE_SECONDS` are
requeued by a reaper that runs at worker start and periodically afterwards.

**Idempotent submission.** `(owner_id, idempotency_key)` is unique at the database
level, so a client retry cannot create a second job.

**Secrets.** `JWT_SECRET` has a development default that the app refuses to use
when `ENVIRONMENT=prod`.

## Limitations / next steps

Deliberate scope boundaries, not hidden gaps:

- Schema is created with `create_all`; a real deployment should use Alembic
  migrations.
- A `running` job cannot be cancelled mid-flight; handlers should be short.
- No per-user rate limiting or quotas.
- No metrics/tracing endpoint (that belongs to the monitoring project).
- Webhook callbacks on completion are not implemented.

## Layout

```
app/
  main.py          application factory, routers, dependency wiring
  config.py        typed settings + production safety checks
  db.py            engine, session factory, declarative base
  models.py        User and Job tables
  security.py      bcrypt hashing, JWT, auth dependencies
  schemas.py       request/response models
  handlers.py      job type registry + implementations
  broker.py        Redis wake-up channel
  worker.py        claim → execute → record/retry loop
  routers/         auth, jobs, admin, health
  cli.py           init-db, create-user
tests/             API, worker, security and handler tests
docs/              architecture notes
```
