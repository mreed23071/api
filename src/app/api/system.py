"""Liveness and readiness probes.

Deliberately unversioned and mounted at the root: these are infrastructure
contracts with the orchestrator, not part of the API that clients consume, and
they must not change shape when the API version does.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.api.deps import SettingsDep
from app.core.db.engine import get_sessionmaker
from app.shared.embeddings.factory import get_embedding_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: str
    database: bool
    embeddings: bool


@router.get("/health", summary="Liveness probe", response_model=HealthResponse)
async def get_health(settings: SettingsDep) -> HealthResponse:
    """Is the process alive? No I/O, so this never fails for a downstream reason."""
    return HealthResponse(status="ok", version=__version__, environment=settings.app_env)


@router.get("/ready", summary="Readiness probe", response_model=ReadinessResponse)
async def get_readiness(response: Response) -> ReadinessResponse:
    """Can this instance serve traffic?

    Returns **503** when degraded, not 200-with-a-status-field: orchestrators
    route on the status code, and a probe that always answers 200 is a probe
    that never removes a broken pod from rotation.
    """
    database_ok = True
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # pragma: no cover - infrastructure failure path
        logger.warning("readiness: database check failed")
        database_ok = False

    # Check that the worker pool is up rather than encoding a string: a real
    # forward pass on every probe is a permanent background CPU cost and holds
    # an executor slot that ingestion wants.
    embeddings_ok = get_embedding_client().is_ready

    ready = database_ok and embeddings_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ok" if ready else "degraded",
        database=database_ok,
        embeddings=embeddings_ok,
    )
