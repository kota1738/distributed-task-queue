"""Test fixtures.

Each test gets its own SQLite database file and an in-process fake Redis, so the
suite runs with no external services. CI additionally runs the same tests against
real Postgres and Redis (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import os

import fakeredis
import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine

from app.broker import Broker
from app.config import Settings
from app.db import Base
from app.main import create_app
from app.models import User, UserRole
from app.security import hash_password

PASSWORD = "correct-horse-battery"
ALICE = "alice@example.com"
BOB = "bob@example.com"
ADMIN = "admin@example.com"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings for one test.

    Defaults to a throwaway SQLite file so the suite needs no services. Set
    ``TEST_DATABASE_URL`` to run the same suite against Postgres (CI does).
    """
    external_url = os.environ.get("TEST_DATABASE_URL")
    if external_url:
        engine = create_engine(external_url)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        engine.dispose()
        database_url = external_url
    else:
        database_url = f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}"

    return Settings(
        database_url=database_url,
        redis_url=os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/0"),
        jwt_secret="test-secret-not-used-in-prod-but-long-enough",
        environment="dev",
        access_token_ttl_seconds=600,
        retry_base_delay_seconds=0.0,
        worker_poll_interval_seconds=0.01,
    )


@pytest.fixture
def redis_client():
    """In-process fake Redis by default; real Redis when TEST_REDIS_URL is set."""
    external_url = os.environ.get("TEST_REDIS_URL")
    if external_url:
        client = Redis.from_url(external_url, decode_responses=True)
        client.flushdb()
        yield client
        client.flushdb()
        client.close()
        return
    yield fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def broker(redis_client) -> Broker:
    return Broker(client=redis_client)


@pytest.fixture
def client(settings: Settings, broker: Broker):
    app = create_app(settings, broker=broker)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_factory(client: TestClient):
    return client.app.state.session_factory


@pytest.fixture
def users(session_factory) -> dict[str, User]:
    with session_factory() as session:
        created = {
            "alice": User(email=ALICE, password_hash=hash_password(PASSWORD)),
            "bob": User(email=BOB, password_hash=hash_password(PASSWORD)),
            "admin": User(email=ADMIN, password_hash=hash_password(PASSWORD), role=UserRole.admin),
        }
        session.add_all(created.values())
        session.commit()
        for user in created.values():
            session.refresh(user)
        session.expunge_all()
        return created


def _token(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post("/api/v1/auth/token", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def alice_headers(client: TestClient, users) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, ALICE)}"}


@pytest.fixture
def bob_headers(client: TestClient, users) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, BOB)}"}


@pytest.fixture
def admin_headers(client: TestClient, users) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, ADMIN)}"}


def submit(client: TestClient, headers: dict[str, str], **body):
    payload = {"type": "echo", "payload": {"value": "hello"}, **body}
    return client.post("/api/v1/jobs", json=payload, headers=headers)
