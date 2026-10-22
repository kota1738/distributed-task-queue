"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_db_engine(url: str) -> Engine:
    """Build an engine for the given SQLAlchemy URL.

    SQLite is supported for tests and single-process local runs; Postgres is the
    supported production database. Threads must share one connection for
    in-memory SQLite, hence ``StaticPool``.
    """
    if url.startswith("sqlite"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if ":memory:" in url else None,
            future=True,
        )
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def init_models(engine: Engine) -> None:
    """Create missing tables. Enough for local development and CI."""
    Base.metadata.create_all(engine)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session and always close it."""
    session = factory()
    try:
        yield session
    finally:
        session.close()
