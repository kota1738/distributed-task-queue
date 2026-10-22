"""Health probes for container orchestration."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import text

from app import metrics
from app.deps import BrokerDep, SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint. Unauthenticated, like the other probes."""
    return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)


@router.get("/healthz")
def liveness() -> dict[str, str]:
    """Process is up. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
def readiness(session: SessionDep, broker: BrokerDep, response: Response) -> dict[str, object]:
    """Ready to serve traffic: database reachable and broker answering."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        logger.warning("readiness: database check failed: %s", exc)
        checks["database"] = "unavailable"
        healthy = False

    try:
        broker.ping()
        checks["broker"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness: broker check failed: %s", exc)
        checks["broker"] = "unavailable"
        healthy = False

    response.status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}
