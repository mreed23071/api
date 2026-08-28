"""v1 wire contracts for the ingestion domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domains.identity.models import Platform
from app.domains.ingestion.dto import (
    ConnectorHealth,
    ConnectorStatus,
    FilterDecision as FilterDecisionDto,
    IngestionOptions,
    IngestionRunResult,
)
from app.domains.ingestion.models import IngestionRun


class FilterDecisionRead(BaseModel):
    """One verdict from the filtering agent."""

    id: str
    keep: bool
    category: str
    reason: str | None = None
    is_fallback: bool = Field(
        description="True when the agent failed and this is the fail-closed default, "
        "not a real judgement."
    )

    @classmethod
    def from_dto(cls, decision: FilterDecisionDto) -> "FilterDecisionRead":
        return cls(**decision.model_dump())


class IngestionRunRequest(BaseModel):
    """Optional per-run overrides for the scheduler."""

    limit: int | None = Field(
        default=None, ge=1, le=1000, description="Cap on messages pulled this run."
    )
    system_prompt_override: str | None = Field(
        default=None,
        description=(
            "Overrides INGESTION_FILTER_SYSTEM_PROMPT for this run only. "
            "Intended for tuning against dry_run; changing retention policy "
            "for a persisted run should be a configuration change, not a "
            "request parameter."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Run source, filtering and embedding, then roll back instead of persisting.",
    )

    def to_options(self) -> IngestionOptions:
        return IngestionOptions(
            limit=self.limit,
            system_prompt_override=self.system_prompt_override,
            dry_run=self.dry_run,
        )


class IngestionRunResponse(BaseModel):
    """Machine-readable run report for the scheduler's logs and alerting."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    dry_run: bool

    fetched: int = Field(description="Messages returned by the source connector.")
    already_ingested: int = Field(description="Skipped: previously stored.")
    evaluated: int = Field(description="Messages sent to the filtering agent.")
    retained: int = Field(description="Classified as worth keeping.")
    discarded: int = Field(description="Rejected by the policy.")
    filter_errors: int = Field(
        description="Messages dropped because the agent failed, not because the "
        "policy rejected them. Alert on this."
    )
    embedded: int = Field(description="Vectors generated locally this run.")
    persisted: int = Field(description="Rows actually written.")
    users_provisioned: int
    relations_provisioned: int

    filter_provider: str
    filter_prompt_version: str
    embedding_model: str
    decisions: list[FilterDecisionRead] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: IngestionRunResult) -> "IngestionRunResponse":
        return cls(
            run_id=result.run_id,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            dry_run=result.dry_run,
            fetched=result.fetched,
            already_ingested=result.already_ingested,
            evaluated=result.evaluated,
            retained=result.retained,
            discarded=result.discarded,
            filter_errors=result.filter_errors,
            embedded=result.embedded,
            persisted=result.persisted,
            users_provisioned=result.users_provisioned,
            relations_provisioned=result.relations_provisioned,
            filter_provider=result.filter_provider,
            filter_prompt_version=result.filter_prompt_version,
            embedding_model=result.embedding_model,
            decisions=[FilterDecisionRead.from_dto(d) for d in result.decisions],
        )


class IngestionConfigResponse(BaseModel):
    """The knobs the ingestion pipeline is currently running with."""

    filter_system_prompt: str
    prompt_version: str
    llm_provider: str
    embedding_model: str
    embedding_dim: int
    embedding_executor: str
    embedding_workers: int
    source: str


class IngestionRunSummary(BaseModel):
    """One past run, as the history list shows it.

    Built from the stored row rather than from the in-memory result, so the
    history screen and a run's own response cannot drift apart.
    """

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID = Field(validation_alias="id")
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int
    dry_run: bool
    status: str = Field(description='"success", "partial" or "failed".')

    fetched: int
    already_ingested: int
    evaluated: int
    retained: int
    discarded: int
    embedded: int
    persisted: int
    users_provisioned: int
    filter_errors: int

    filter_provider: str | None = None
    embedding_model: str | None = None
    decisions: list[FilterDecisionRead] = Field(default_factory=list)

    @classmethod
    def from_entity(cls, run: IngestionRun) -> "IngestionRunSummary":
        """Build from a stored row, including its filtering decisions.

        `validation_alias="id"` maps the row's primary key onto the `run_id`
        the console already expects, so neither side has to rename anything.
        """
        return cls(
            id=run.id,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_ms=run.duration_ms,
            dry_run=run.dry_run,
            status=run.status,
            fetched=run.fetched,
            already_ingested=run.already_ingested,
            evaluated=run.evaluated,
            retained=run.retained,
            discarded=run.discarded,
            embedded=run.embedded,
            persisted=run.persisted,
            users_provisioned=run.users_provisioned,
            filter_errors=run.filter_errors,
            filter_provider=run.filter_provider,
            embedding_model=run.embedding_model,
            decisions=[
                FilterDecisionRead(
                    id=decision.external_message_id,
                    keep=decision.keep,
                    category=decision.category or "unknown",
                    reason=decision.reason,
                )
                for decision in run.decisions
            ],
        )


class ConnectorRead(BaseModel):
    """One platform's contribution, as the integrations screen shows it.

    `status` is inferred from stored data - account count and how recently
    anything arrived. Nothing polls a connector or checks a credential, because
    no real connector exists yet, so a platform can read "connected" here while
    its token expired an hour ago.
    """

    platform: Platform
    status: ConnectorStatus
    last_sync_at: datetime | None = None
    messages_contributed: int = 0
    account_count: int = 0

    @classmethod
    def from_dto(cls, health: ConnectorHealth) -> "ConnectorRead":
        return cls(
            platform=health.platform,
            status=health.status,
            last_sync_at=health.last_sync_at,
            messages_contributed=health.messages_contributed,
            account_count=health.account_count,
        )
