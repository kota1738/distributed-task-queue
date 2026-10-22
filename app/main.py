"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from sqlalchemy import Engine

from app import __version__, metrics
from app.broker import Broker
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory, init_models
from app.routers import admin, auth, health, jobs
from app.schemas import JobTypeResponse

logger = logging.getLogger(__name__)

DESCRIPTION = """
Asynchronous job processing service.

* Submit jobs over HTTP and track them by id.
* Workers claim jobs from Postgres with `FOR UPDATE SKIP LOCKED`; Redis only
  signals that work is available.
* Failed jobs retry with exponential backoff until `max_attempts` is reached.
"""


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    broker: Broker | None = None,
) -> FastAPI:
    """Build the application. Dependencies can be injected for tests."""
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_settings.validate_for_runtime()
        db_engine = engine or create_db_engine(app_settings.database_url)
        init_models(db_engine)
        app.state.engine = db_engine
        app.state.session_factory = create_session_factory(db_engine)
        metrics.bind_session_factory(app.state.session_factory)
        app.state.broker = broker or Broker(
            app_settings.redis_url,
            app_settings.queue_name,
            app_settings.processing_queue_name,
        )
        logger.info("application ready (environment=%s)", app_settings.environment)
        try:
            yield
        finally:
            if broker is None:
                app.state.broker.close()
            if engine is None:
                db_engine.dispose()

    app = FastAPI(
        title="Distributed Task Queue API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: app_settings

    api = APIRouter(prefix="/api/v1")
    api.include_router(auth.router)
    api.include_router(jobs.router)
    api.include_router(admin.router)
    api.add_api_route(
        "/job-types",
        jobs.job_type_catalogue,
        methods=["GET"],
        response_model=list[JobTypeResponse],
        tags=["jobs"],
        summary="List the job types this deployment can execute",
    )
    app.include_router(api)
    app.include_router(health.router)
    return app


app = create_app()
