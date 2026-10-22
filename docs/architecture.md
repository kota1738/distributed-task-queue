# Architecture notes

## Components

| Component | Responsibility | Scale-out |
| --- | --- | --- |
| `app.main` (FastAPI) | Auth, job submission, status and result reads, admin listing | Stateless; run N replicas behind a load balancer |
| `app.worker` | Claims due jobs, executes handlers, records outcomes | Run N replicas; claims are atomic |
| PostgreSQL | Source of truth for users and jobs | Managed instance / standby |
| Redis | Wake-up channel and processing list | Optional for correctness |

## Submission lifecycle

1. Client `POST /api/v1/jobs` with a bearer token.
2. The route resolves the job type, validates the payload against its pydantic
   model, and inserts a `queued` row (`next_run_at = now`).
3. The route `LPUSH`es the job id onto `taskqueue:jobs`. If Redis is unavailable
   the request still succeeds — the job is durable, workers will poll it.
4. A worker blocked in `BRPOPLPUSH` wakes, moves the signal to
   `taskqueue:jobs:processing`, then claims **the oldest due job** from Postgres
   (not necessarily the signalled id). After the handler finishes, the signal is
   `LREM`ed.

The indirection in step 4 is deliberate: a signal only means "work may exist".
Duplicated, stale or missing signals change latency, never correctness.

## Claim query

```python
select(Job.id)
  .where(Job.status == "queued", Job.next_run_at <= now)
  .order_by(Job.next_run_at, Job.created_at)
  .limit(1)
  .with_for_update(skip_locked=True)
```

followed by

```python
update(Job)
  .where(Job.id == job_id, Job.status == "queued")
  .values(status="running", attempts=Job.attempts + 1, started_at=now)
  .returning(Job.id)
```

Postgres renders `FOR UPDATE SKIP LOCKED`, so workers take different rows and
never queue behind each other. SQLite ignores the clause; the `status = 'queued'`
guard keeps the claim single-winner there.

## Data model

`users`

| Column | Notes |
| --- | --- |
| `id` | integer primary key |
| `email` | unique, stored lower-case |
| `password_hash` | bcrypt |
| `role` | `user` / `admin` |
| `is_active` | deactivated users are rejected even with a valid token |

`jobs`

| Column | Notes |
| --- | --- |
| `id` | UUID primary key |
| `owner_id` | FK → `users.id`, `ON DELETE CASCADE` |
| `type`, `payload` | job type and validated JSON payload |
| `status` | `queued`, `running`, `succeeded`, `failed`, `cancelled` |
| `attempts`, `max_attempts` | attempts increments at claim time |
| `result`, `error` | outcome of the last attempt |
| `idempotency_key` | unique together with `owner_id` |
| `created_at`, `updated_at`, `started_at`, `finished_at`, `next_run_at` | naive UTC |

Indexes: `(status, next_run_at)` for claims, `(owner_id, created_at)` for
listings.

## Failure scenarios

| Scenario | Behaviour |
| --- | --- |
| Handler raises a transient error | Row returns to `queued` with backoff; fails after `max_attempts` |
| Handler raises a permanent error (unknown type) | Row fails immediately, no retries burnt |
| Worker crashes mid-job | Row stays `running`; the lease reaper requeues it after `WORKER_LEASE_SECONDS` |
| Redis unavailable at submission | `201` still returned; workers poll Postgres |
| Redis unavailable to a worker | Worker logs a warning and falls back to DB polling |
| Postgres unavailable | Readiness reports `503`; requests fail fast |
| Duplicate submission | `Idempotency-Key` returns the original job with `200` |
| Two workers, one job | Exactly one claim succeeds; the other worker takes a different job or idles |

## Security model

- Passwords are bcrypt hashes; the 72-byte bcrypt limit is enforced.
- Tokens are HS256 JWTs with `sub`, `role`, `iat`, `exp`, `jti`; expiry and
  signature failures return `401` with `WWW-Authenticate: Bearer`.
- Ownership is enforced in queries; another user's job returns `404`, not `403`,
  so job ids cannot be probed.
- Handlers receive a plain dict payload that was validated at submission time.

## Why not Celery or RQ?

Those frameworks hide the mechanics this project is meant to demonstrate. Here
the claim, retry, lease and idempotency behaviour is explicit and testable in
about 400 lines, which is far more useful for understanding — and for interview
discussion — than a thin wrapper around a task library.
