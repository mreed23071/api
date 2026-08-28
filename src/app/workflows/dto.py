"""What crosses the workflow/activity boundary.

Every one of these is serialized into Temporal's event history, so they are
pydantic models rather than domain objects holding sessions or clients. They
are also the *durable* record of a run's intermediate state: the reason a
worker can die mid-pipeline and resume without re-running the model is that
`FilterOutcome` and `EmbedOutcome` are already in history by then.

Keep them additive. A field removed here breaks workflows that are mid-flight
against the old shape.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domains.identity.models import Platform
from app.domains.ingestion.dto import FilterDecision, RawMessage


class IngestionInput(BaseModel):
    """The workflow argument. Mirrors the trigger endpoint's request body."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    platform: Platform
    limit: int | None = None
    system_prompt_override: str | None = None
    dry_run: bool = False
    #: Scopes are checked at the edge, before the workflow starts. The worker
    #: acts as the pipeline itself and re-derives a service principal, so the
    #: caller's credential never has to be serialized into history.
    requested_by: str = "unknown"


class FetchOutcome(BaseModel):
    """Everything the connector returned that is not already stored.

    Also carries the run's provenance - which model will judge, under which
    prompt version. Workflow code cannot read settings (it has to replay
    identically), so the first activity reports them and the workflow carries
    them through to the run record.
    """

    candidates: list[RawMessage] = Field(default_factory=list)
    fetched: int = 0
    already_ingested: int = 0
    filter_provider: str = ""
    filter_prompt_version: str = ""


class FilterOutcome(BaseModel):
    """One batch's verdicts. Durable, so a resumed run does not re-classify."""

    decisions: list[FilterDecision] = Field(default_factory=list)


class EmbedOutcome(BaseModel):
    """Vectors for one batch, positionally aligned with the texts sent."""

    vectors: list[list[float]] = Field(default_factory=list)
    model: str = ""


class PersistInput(BaseModel):
    """The single write. Carries everything the transaction needs."""

    retained: list[RawMessage] = Field(default_factory=list)
    vectors: list[list[float]] = Field(default_factory=list)
    decisions: list[FilterDecision] = Field(default_factory=list)
    embedding_model: str = ""
    filter_prompt_version: str = ""
    dry_run: bool = False


class PersistOutcome(BaseModel):
    persisted: int = 0
    users_provisioned: int = 0
    relations_provisioned: int = 0


class RunSummary(BaseModel):
    """The workflow's result, and what the status endpoint reports.

    Deliberately the same counters as `IngestionRunResult`, so the queued path
    and the synchronous one describe a run identically.
    """

    run_id: str
    platform: Platform
    started_at: datetime
    finished_at: datetime
    duration_ms: int = 0
    dry_run: bool = False
    fetched: int = 0
    already_ingested: int = 0
    evaluated: int = 0
    retained: int = 0
    discarded: int = 0
    filter_errors: int = 0
    embedded: int = 0
    persisted: int = 0
    users_provisioned: int = 0
    relations_provisioned: int = 0
    filter_provider: str = ""
    filter_prompt_version: str = ""
    embedding_model: str = ""
    decisions: list[FilterDecision] = Field(default_factory=list)


class RunProgress(BaseModel):
    """Answered by a workflow query, so the UI can show real progress.

    The console used to animate fake pipeline steps because the API had nothing
    to report until the whole run finished. This is the real thing.
    """

    stage: str = "starting"
    fetched: int = 0
    evaluated: int = 0
    filtered: int = 0
    embedded: int = 0
    persisted: int = 0
