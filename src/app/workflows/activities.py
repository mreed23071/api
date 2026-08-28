"""The side-effecting half of ingestion.

Everything that touches the network, the model or the database lives here.
Workflow code may not do any of it - it has to be replayable - so this module
is where the existing domain services are actually called.

Each activity builds its own session and clients. Activities run in the worker
process, not in a request, so there is no dependency-injection graph to borrow
from; `Deps` below is the composition root for that process, cached so the
embedding pool and HTTP transports are created once rather than per activity.

Note what is *not* here: no activity spans two pipeline stages. That is the
whole point - each one's result lands in Temporal's history, so a crash costs
at most the activity in flight, never the model calls already paid for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from temporalio import activity

from app.core.config import Settings, get_settings
from app.core.db.engine import get_sessionmaker
from app.core.security.principal import Principal, PrincipalKind, Scope, TenantContext
from app.domains.identity.service import IdentityService
from app.domains.ingestion.dto import RawMessage
from app.domains.ingestion.filtering import MessageFilterAgent
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision
from app.domains.ingestion.sources import source_for
from app.domains.messaging.dto import NewMessage
from app.domains.messaging.service import MessageService
from app.domains.uow import UnitOfWork
from app.shared.embeddings.base import EmbeddingClient
from app.shared.embeddings.factory import get_embedding_client
from app.shared.llm.base import LLMClient
from app.shared.llm.factory import get_llm_client
from app.workflows.dto import (
    EmbedOutcome,
    FetchOutcome,
    FilterOutcome,
    IngestionInput,
    PersistInput,
    PersistOutcome,
    RunSummary,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Deps:
    """Process-wide collaborators for the worker."""

    settings: Settings
    llm: LLMClient
    embeddings: EmbeddingClient


@lru_cache(maxsize=1)
def get_deps() -> Deps:
    settings = get_settings()
    embeddings = get_embedding_client()
    embeddings.start()
    return Deps(settings=settings, llm=get_llm_client(), embeddings=embeddings)


def worker_principal() -> Principal:
    """The pipeline acting as itself.

    The caller's scopes were checked at the edge before the workflow started.
    Re-deriving a service principal here keeps credentials out of the workflow
    history, which is durable and readable in the Temporal UI.
    """
    return Principal(
        subject="ingestion-worker",
        kind=PrincipalKind.SERVICE,
        scopes=frozenset({Scope.INGEST_RUN, Scope.INGEST_READ}),
        tenant=TenantContext.global_scope(),
        auth_scheme="workflow",
    )


def _uow(session) -> UnitOfWork:  # type: ignore[no-untyped-def]
    return UnitOfWork(session, TenantContext.global_scope())


# -- activities ------------------------------------------------------------


@activity.defn
async def fetch_candidates(payload: IngestionInput) -> FetchOutcome:
    """Pull from the connector and drop anything already stored.

    Idempotent: re-running it after a crash returns the same candidates minus
    whatever a previous attempt managed to persist, which is exactly right.
    """
    source = source_for(payload.platform)
    fetched = list(await source.fetch(limit=payload.limit))

    async with get_sessionmaker()() as session:
        messages = MessageService(_uow(session), worker_principal())
        unseen = set(await messages.unseen([message.key for message in fetched]))

    candidates = [message for message in fetched if message.key in unseen]
    activity.logger.info("fetched %d, %d new", len(fetched), len(candidates))

    deps = get_deps()
    return FetchOutcome(
        candidates=candidates,
        fetched=len(fetched),
        already_ingested=len(fetched) - len(candidates),
        filter_provider=deps.llm.provider,
        filter_prompt_version=deps.settings.prompt_version,
    )


@activity.defn
async def filter_batch(batch: list[RawMessage]) -> FilterOutcome:
    """Classify one batch. The expensive step, and the reason for this design.

    One activity per batch rather than one for the whole run: a batch that has
    been judged is recorded in history, so a worker that dies half way through
    a run resumes without paying the model for the batches already done.
    """
    deps = get_deps()
    agent = MessageFilterAgent(deps.llm, system_prompt=deps.settings.ingestion_filter_system_prompt)
    decisions = await agent.filter(batch)
    return FilterOutcome(decisions=decisions)


@activity.defn
async def embed_batch(texts: list[str]) -> EmbedOutcome:
    """Vectorise one batch, in the order given."""
    deps = get_deps()
    vectors = await deps.embeddings.embed(texts)
    return EmbedOutcome(vectors=vectors, model=deps.embeddings.model_name)


@activity.defn
async def persist(payload: PersistInput) -> PersistOutcome:
    """Provision identities and store messages, in one transaction.

    Safe to retry: the message insert is `ON CONFLICT DO NOTHING` on
    `(platform, external_message_id)`, and identity provisioning resolves an
    existing person rather than creating a second one.
    """
    if payload.dry_run:
        # Every preceding stage ran for real; this is where a dry run stops.
        return PersistOutcome()

    verdicts = {decision.id: decision for decision in payload.decisions}

    async with get_sessionmaker()() as session:
        uow = _uow(session)
        principal = worker_principal()
        identity = IdentityService(uow, principal)
        messages = MessageService(uow, principal)

        async with uow.transaction():
            resolution = await identity.resolve_or_provision(
                [message.as_identity_candidate() for message in payload.retained]
            )
            rows = []
            for message, vector in zip(payload.retained, payload.vectors, strict=True):
                relation = resolution[(message.platform, message.external_author_id)]
                decision = verdicts[message.external_message_id]
                rows.append(
                    NewMessage(
                        platform=message.platform,
                        external_message_id=message.external_message_id,
                        conversation_id=message.conversation_id,
                        content=message.content,
                        sent_at=message.sent_at,
                        kind=message.kind,
                        source_metadata=message.metadata,
                        sender_user_id=relation.user_id,
                        sender_relation_id=relation.id,
                        embedding=list(vector),
                        embedding_model=payload.embedding_model,
                        filter_category=decision.category,
                        filter_reason=decision.reason,
                        filter_prompt_version=payload.filter_prompt_version,
                    )
                )
            persisted = await messages.store(rows)

        return PersistOutcome(
            persisted=len(persisted),
            users_provisioned=resolution.users_created,
            relations_provisioned=resolution.relations_created,
        )


@activity.defn
async def record_run(summary: RunSummary) -> None:
    """Write the run to history.

    Its own activity, and last, so that a failure to record does not roll back
    a successful ingestion - and so a retry of the recording does not re-run
    the pipeline.
    """
    async with get_sessionmaker()() as session:
        uow = _uow(session)
        async with uow.transaction():
            run = IngestionRun(
                started_at=summary.started_at,
                finished_at=summary.finished_at,
                duration_ms=summary.duration_ms,
                dry_run=summary.dry_run,
                platform=summary.platform,
                fetched=summary.fetched,
                already_ingested=summary.already_ingested,
                evaluated=summary.evaluated,
                retained=summary.retained,
                discarded=summary.discarded,
                embedded=summary.embedded,
                persisted=summary.persisted,
                users_provisioned=summary.users_provisioned,
                filter_errors=summary.filter_errors,
                filter_provider=summary.filter_provider,
                embedding_model=summary.embedding_model,
                status=_status_of(summary),
            )
            run.decisions = [
                IngestionRunDecision(
                    external_message_id=decision.id,
                    keep=decision.keep,
                    category=decision.category,
                    reason=decision.reason,
                    is_fallback=decision.is_fallback,
                )
                for decision in summary.decisions
            ]
            await uow.runs.add(run)


def _status_of(summary: RunSummary) -> str:
    """Same rule as the synchronous pipeline's `_status_of`."""
    if summary.filter_errors and summary.retained and not summary.persisted:
        return "failed"
    if summary.filter_errors:
        return "partial"
    return "success"


#: Registered with the Worker. A new activity missing from this list makes
#: the workflow that calls it hang rather than fail, so it is asserted in
#: tests/unit/workflows/test_ingestion_workflow.py.
ALL_ACTIVITIES: list[Callable[..., Any]] = [
    fetch_candidates,
    filter_batch,
    embed_batch,
    persist,
    record_run,
]
