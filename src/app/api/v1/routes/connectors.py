"""v1 HTTP surface for platform connector health."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import IngestionServiceDep
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.ingestion import ConnectorRead

router = APIRouter(prefix="/connectors", tags=["ingestion"], responses=AUTH_RESPONSES)


@router.get(
    "",
    response_model=list[ConnectorRead],
    summary="List every platform and what it has contributed",
    description=(
        "One row per platform, including platforms with nothing on them - a "
        "platform silently missing from this list would look identical to one "
        "that is working.\n\n"
        "`status` is **inferred** from stored data: how many accounts exist and "
        "how recently anything arrived. Nothing polls a connector or checks a "
        "credential, because no real connector exists yet, so a platform can "
        "read `connected` here while its token expired an hour ago."
    ),
)
async def list_connectors(service: IngestionServiceDep) -> list[ConnectorRead]:
    return [ConnectorRead.from_dto(health) for health in await service.connectors()]
