"""v1 HTTP surface for Domain 1 - ingestion & embedding."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import IngestionServiceDep, MessageSourceDep, SettingsDep
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.ingestion import (
    IngestionConfigResponse,
    IngestionRunRequest,
    IngestionRunResponse,
    IngestionRunSummary,
)
from app.core.security.dependencies import require_scopes
from app.core.security.principal import Scope

router = APIRouter(prefix="/ingestion", tags=["ingestion"], responses=AUTH_RESPONSES)


@router.post(
    "/runs",
    response_model=IngestionRunResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_scopes(Scope.INGEST_RUN))],
    summary="Run one ingestion cycle",
    description=(
        "Invoked by the external scheduler. Pulls messages from the configured "
        "source, filters them with the agentic policy, generates embeddings "
        "locally on a worker pool, and stores the survivors.\n\n"
        "Idempotent: messages already stored are skipped by "
        "`(platform, external_message_id)`, so retries and overlapping windows "
        "are safe.\n\n"
        "Synchronous - the response waits for the whole pipeline. Acceptable for "
        "the fixture connector; a real one needs this to become a queued job."
    ),
)
async def run_ingestion(
    service: IngestionServiceDep,
    payload: IngestionRunRequest | None = None,
) -> IngestionRunResponse:
    request = payload or IngestionRunRequest()
    result = await service.run(request.to_options())
    return IngestionRunResponse.from_result(result)


@router.get(
    "/config",
    response_model=IngestionConfigResponse,
    dependencies=[Depends(require_scopes(Scope.INGEST_READ))],
    summary="Inspect the active ingestion configuration",
    description="Confirms which prompt, model and executor a deployment picked up.",
)
async def get_ingestion_config(
    settings: SettingsDep,
    source: MessageSourceDep,
) -> IngestionConfigResponse:
    return IngestionConfigResponse(
        filter_system_prompt=settings.ingestion_filter_system_prompt,
        prompt_version=settings.prompt_version,
        llm_provider=settings.llm_provider,
        embedding_model=settings.embedding_model_name,
        embedding_dim=settings.embedding_dim,
        embedding_executor=settings.embedding_executor,
        embedding_workers=settings.embedding_workers,
        source=source.name,
    )


@router.get(
    "/runs",
    response_model=list[IngestionRunSummary],
    summary="List past ingestion runs",
    description=(
        "Newest first, each with the filtering verdicts it recorded. That trail "
        "is what makes tuning the filter policy a reviewable act rather than a "
        "guess - it is the evidence behind every retention decision.\n\n"
        "Dry runs appear here too. They deliberately store no messages, but the "
        "record of what they *would* have stored is the entire point of running "
        "one."
    ),
)
async def list_ingestion_runs(
    service: IngestionServiceDep,
    limit: Annotated[int, Query(ge=1, le=100, description="How many runs to return.")] = 20,
) -> list[IngestionRunSummary]:
    return [
        IngestionRunSummary.from_entity(run) for run in await service.history(limit=limit)
    ]
