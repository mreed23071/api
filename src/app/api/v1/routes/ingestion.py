"""v1 HTTP surface for Domain 1 - ingestion & embedding."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import IngestionServiceDep, MessageSourceDep, SettingsDep, get_message_source
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.ingestion import (
    ActiveRunRead,
    ActiveRunsResponse,
    IngestionConfigResponse,
    IngestionRunRequest,
    IngestionRunResponse,
    IngestionRunSummary,
    QueuedRunResponse,
    RunProgressResponse,
)
from app.core.errors import NotFoundError
from app.core.security.dependencies import require_scopes
from app.core.security.principal import Scope
from app.domains.identity.models import Platform
from app.workflows.dto import IngestionInput
from app.workflows.gateway import describe_ingestion_run, list_active_runs, start_ingestion_run

router = APIRouter(prefix="/ingestion", tags=["ingestion"], responses=AUTH_RESPONSES)

PlatformPath = Annotated[Platform, Path(description="Which platform's pipeline to run.")]


@router.post(
    "/runs/{platform}",
    response_model=QueuedRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scopes(Scope.INGEST_RUN))],
    summary="Queue one platform's ingestion cycle",
    description=(
        "One pipeline per platform, invoked by the external scheduler "
        "independently per platform. Pulls messages from that platform's "
        "connector, filters them with the agentic policy, generates "
        "embeddings, and stores the survivors.\n\n"
        "**Accepted, not completed.** The run is handed to a durable workflow "
        "and this returns immediately with a `run_id`. Poll "
        "`GET /ingestion/runs/{platform}/{run_id}` for stage-by-stage "
        "progress and the final counters. Inference against a local model "
        "takes minutes, so waiting for it in the request would time out in a "
        "browser long before there was anything to report.\n\n"
        "Idempotent twice over: messages already stored are skipped by "
        "`(platform, external_message_id)`, and the workflow id derived from "
        "`run_id` means the same run cannot be started concurrently.\n\n"
        "404 if no connector is configured for the platform yet."
    ),
)
async def run_ingestion(
    platform: PlatformPath,
    service: IngestionServiceDep,
    settings: SettingsDep,
    payload: IngestionRunRequest | None = None,
) -> QueuedRunResponse:
    request = payload or IngestionRunRequest()
    # An explicit `limit` always wins; otherwise fall back to what the current
    # fixtures mode implies (500 in bulk, connector default otherwise) rather
    # than leaving it unset. See `Settings.ingestion_default_limit`.
    limit = request.limit if request.limit is not None else settings.ingestion_default_limit

    # Resolve the connector before queueing, so an unconfigured platform is a
    # 404 here rather than a workflow that starts and immediately fails.
    get_message_source(platform)

    if not settings.temporal_enabled:
        # No orchestrator configured - run it inline and report it as already
        # finished. This is the path the test suite and a bare `uvicorn` take.
        options = request.to_options()
        options = replace(options, limit=limit)
        result = await service.run(options, platform=platform)
        return QueuedRunResponse(
            run_id=result.run_id,
            platform=platform,
            status="completed",
            workflow_id="inline",
            dry_run=result.dry_run,
        )

    run_id = str(uuid.uuid4())
    handle = await start_ingestion_run(
        IngestionInput(
            run_id=run_id,
            platform=platform,
            limit=limit,
            system_prompt_override=request.system_prompt_override,
            dry_run=request.dry_run,
        )
    )
    return QueuedRunResponse(
        run_id=run_id,
        platform=platform,
        workflow_id=handle.id,
        dry_run=request.dry_run,
    )


@router.get(
    "/runs/{platform}/{run_id}",
    response_model=RunProgressResponse,
    dependencies=[Depends(require_scopes(Scope.INGEST_READ))],
    summary="Poll a queued run",
    description=(
        "Stage-by-stage progress while a run is in flight, and the full report "
        "once it finishes. `status` is one of queued, running, completed, "
        "failed or cancelled; `result` is populated only on completion.\n\n"
        "Progress comes from a workflow query, which reads live state without "
        "replaying history, so polling this is cheap."
    ),
)
async def get_run_status(
    platform: PlatformPath,
    run_id: Annotated[str, Path(description="The id returned when the run was queued.")],
    settings: SettingsDep,
) -> RunProgressResponse:
    if not settings.temporal_enabled:
        raise NotFoundError(
            "Run status is only available when ingestion is orchestrated by Temporal.",
            details={"run_id": run_id},
        )
    view = await describe_ingestion_run(run_id)
    return RunProgressResponse(
        run_id=view.run_id,
        status=view.status,
        stage=view.progress.stage,
        fetched=view.progress.fetched,
        evaluated=view.progress.evaluated,
        filtered=view.progress.filtered,
        embedded=view.progress.embedded,
        persisted=view.progress.persisted,
        result=IngestionRunResponse.from_summary(view.summary) if view.summary else None,
    )


@router.get(
    "/runs/active",
    response_model=ActiveRunsResponse,
    summary="Is ingestion running right now, anywhere",
    description=(
        "For a console-wide indicator, not a dashboard - just whether at least "
        "one run is currently in flight, on any platform. Reads Temporal's live "
        "state directly rather than the history table, which only gains a row "
        "once a run finishes.\n\n"
        "Always answers `count: 0` when ingestion isn't orchestrated by Temporal "
        "- an inline run has already finished by the time its `POST` responds, "
        "so there's never anything left in flight to report."
    ),
)
async def get_active_runs(settings: SettingsDep) -> ActiveRunsResponse:
    if not settings.temporal_enabled:
        return ActiveRunsResponse(count=0, runs=[])
    active = await list_active_runs()
    return ActiveRunsResponse(
        count=len(active),
        runs=[
            ActiveRunRead(
                run_id=run.run_id,
                platform=run.platform,
                stage=run.stage,
                started_at=run.started_at,
            )
            for run in active
        ],
    )


@router.get(
    "/config/{platform}",
    response_model=IngestionConfigResponse,
    dependencies=[Depends(require_scopes(Scope.INGEST_READ))],
    summary="Inspect one platform's active ingestion configuration",
    description="Confirms which prompt, model, executor and connector a deployment picked up.",
)
async def get_ingestion_config(
    platform: PlatformPath,
    settings: SettingsDep,
    source: MessageSourceDep,
) -> IngestionConfigResponse:
    return IngestionConfigResponse(
        platform=platform,
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
        "one.\n\n"
        "Unfiltered by default, across every platform. Pass `platform` to see "
        "just one pipeline's history."
    ),
)
async def list_ingestion_runs(
    service: IngestionServiceDep,
    limit: Annotated[int, Query(ge=1, le=100, description="How many runs to return.")] = 20,
    platform: Annotated[
        Platform | None, Query(description="Restrict to one platform's runs.")
    ] = None,
) -> list[IngestionRunSummary]:
    return [
        IngestionRunSummary.from_entity(run)
        for run in await service.history(limit=limit, platform=platform)
    ]
