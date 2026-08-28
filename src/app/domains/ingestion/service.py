"""Ingestion pipeline orchestration (Domain 1).

    source -> dedupe -> agentic filter -> identity resolution -> local
    embedding (off-loop) -> persist

Transaction shape is the thing to notice. The read, the LLM round trips and the
embedding batch all happen *outside* a write transaction; only provisioning and
the final insert are inside one. A transaction held open across a network call
pins a pooled connection and holds row locks for as long as the provider takes
to answer, which is the difference between a slow run and a stalled database.

Still synchronous end to end: the HTTP response waits for the whole pipeline.
That is fine for a fixture connector and wrong for a real one - see R-1 in
`docs/PROTOTYPE-REPORT.md` for the queue-backed shape this should grow into.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.logging import run_id_var
from app.core.security.principal import Principal, Scope
from app.core.security.provisional import require_console_access
from app.domains.identity.dto import IdentityResolution
from app.domains.identity.models import Platform
from app.domains.identity.service import IdentityService
from app.domains.ingestion.dto import (
    ConnectorHealth,
    ConnectorStatus,
    FilterDecision,
    IngestionOptions,
    IngestionRunResult,
    RawMessage,
)
from app.domains.ingestion.filtering import MessageFilterAgent
from app.domains.ingestion.models import IngestionRun, IngestionRunDecision
from app.domains.ingestion.sources import MessageSource
from app.domains.messaging.dto import NewMessage
from app.domains.messaging.service import MessageService
from app.domains.uow import UnitOfWork
from app.shared.embeddings.service import EmbeddingService
from app.shared.llm.base import LLMClient

logger = logging.getLogger(__name__)


class _DryRunRollback(Exception):
    """Internal signal: unwind the write transaction without committing."""


class IngestionService:
    """The pipeline: fetch, filter, resolve identities, embed, store.

    One method does the work - `run` - and the ordering inside it is the design.
    Filtering happens before embedding so nothing personal is ever vectorised,
    and the database transaction is opened as late as possible so a slow model
    call never holds a connection or a lock.
    """

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        principal: Principal,
        source: MessageSource,
        llm: LLMClient,
        embeddings: EmbeddingService,
        settings: Settings,
    ) -> None:
        self.uow = uow
        self.principal = principal
        self.source = source
        self.llm = llm
        self.embeddings = embeddings
        self.settings = settings
        self.identity = IdentityService(uow, principal)
        self.messages = MessageService(uow, principal)

    async def run(self, options: IngestionOptions, *, platform: Platform) -> IngestionRunResult:
        """Execute one ingestion cycle and return a report of what it did.

        `platform` names which pipeline this is - it comes from the route, not
        from `options`, because it is routing-determined (which connector was
        injected) rather than a caller-adjustable override. Recorded on the
        result and the persisted run so the history list can tell pipelines
        apart.

        Five steps, in this order for reasons that matter:

        1. **Fetch** from the connector, and drop anything already stored.
        2. **Filter** each message as business-relevant or personal. Anything
           rejected stops here and is never embedded or stored.
        3. **Record the verdicts**, including how many were fail-closed
           defaults rather than real judgements - so a provider outage is
           visible in the report instead of looking like a strict policy.
        4. **Embed** what survived. This is the slowest step and runs off the
           event loop, still with no transaction open.
        5. **Write**, in one short transaction: provision any new people, then
           store the messages.

        A dry run performs every step and then deliberately raises inside the
        transaction, so everything rolls back. That is how a policy change gets
        tested against real traffic without retaining anything.

        The run is recorded to the history table afterwards, in a separate
        transaction - see `record` for why it cannot share this one.
        """
        self.principal.require(Scope.INGEST_RUN)

        run_id = str(uuid.uuid4())
        token = run_id_var.set(run_id)
        started_at = datetime.now(UTC)
        monotonic_start = time.perf_counter()

        agent = MessageFilterAgent(
            self.llm,
            system_prompt=options.system_prompt_override
            or self.settings.ingestion_filter_system_prompt,
        )
        result = IngestionRunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=started_at,
            duration_ms=0,
            dry_run=options.dry_run,
            platform=platform,
            filter_provider=agent.provider,
            filter_prompt_version=self.settings.prompt_version,
            embedding_model=self.embeddings.model_name,
        )

        try:
            # 1. Pull from the connector. No transaction is open.
            fetched = list(await self.source.fetch(limit=options.limit))
            result.fetched = len(fetched)
            logger.info(
                "source fetch complete",
                extra={"source": self.source.name, "fetched": len(fetched)},
            )

            # 2. Drop anything already stored, then release the read transaction
            #    before doing any slow work.
            unseen = set(await self.messages.unseen([message.key for message in fetched]))
            await self.uow.rollback()

            candidates = [message for message in fetched if message.key in unseen]
            result.already_ingested = len(fetched) - len(candidates)
            result.evaluated = len(candidates)

            # 3. Agentic filtering. Network-bound, no transaction held.
            decisions = await agent.filter(candidates) if candidates else []
            verdicts = {decision.id: decision for decision in decisions}
            retained = [
                message for message in candidates if verdicts[message.external_message_id].keep
            ]
            result.decisions = decisions
            result.retained = len(retained)
            result.discarded = len(candidates) - len(retained)
            result.filter_errors = sum(1 for decision in decisions if decision.is_fallback)
            if result.filter_errors:
                logger.error(
                    "filtering agent fell back to the fail-closed default",
                    extra={"affected": result.filter_errors, "provider": agent.provider},
                )

            # 4. Local embeddings, dispatched off the event loop. Still no
            #    transaction: this is the longest CPU-bound step in the pipeline.
            vectors = await self.embeddings.embed([message.content for message in retained])
            result.embedded = len(vectors)

            # 5. One short write transaction for everything that mutates.
            try:
                async with self.uow.transaction():
                    resolution = await self.identity.resolve_or_provision(
                        [message.as_identity_candidate() for message in retained]
                    )
                    result.users_provisioned = resolution.users_created
                    result.relations_provisioned = resolution.relations_created

                    rows = self._build_rows(retained, vectors, resolution, verdicts)
                    persisted = await self.messages.store(rows)
                    result.persisted = len(persisted)

                    if options.dry_run:
                        raise _DryRunRollback
            except _DryRunRollback:
                result.persisted = 0
                result.users_provisioned = 0
                result.relations_provisioned = 0
                logger.info("dry run complete; all writes rolled back")

            result.finished_at = datetime.now(UTC)
            result.duration_ms = int((time.perf_counter() - monotonic_start) * 1000)
            logger.info(
                "ingestion run complete",
                extra={
                    "fetched": result.fetched,
                    "retained": result.retained,
                    "persisted": result.persisted,
                    "duration_ms": result.duration_ms,
                },
            )
            # Outside the pipeline's transaction on purpose - see `record`.
            await self.record(result)
            return result
        finally:
            run_id_var.reset(token)

    # -- internals ---------------------------------------------------------

    def _build_rows(
        self,
        messages: Sequence[RawMessage],
        vectors: Sequence[Sequence[float]],
        resolution: IdentityResolution,
        verdicts: dict[str, FilterDecision],
    ) -> list[NewMessage]:
        """Combine the three parallel results into rows ready to store.

        By this point three separate steps have each produced a list in the same
        order: the surviving messages, their vectors, and their filter verdicts.
        `zip(..., strict=True)` walks them together and raises if the lengths
        ever disagree - which would mean a vector had been attached to the wrong
        message, and is far better caught here than discovered in the data.
        """
        rows: list[NewMessage] = []
        for message, vector in zip(messages, vectors, strict=True):
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
                    embedding_model=self.embeddings.model_name,
                    filter_category=decision.category,
                    filter_reason=decision.reason,
                    filter_prompt_version=self.settings.prompt_version,
                )
            )
        return rows

    # -- history and health -------------------------------------------------

    async def record(self, result: IngestionRunResult) -> None:
        """Persist a completed run and its filtering decisions.

        Called in its own transaction, deliberately after the pipeline's write
        transaction has closed. A dry run deliberately rolls its transaction
        back, and sharing one would roll the history record back with it - so
        the one run whose record is most worth keeping, because it was testing
        a policy change, would be the one that vanished.

        Failing to record a run must not fail the run. The work already
        happened; losing the receipt is worth a loud log line and nothing more.
        """
        entity = IngestionRun(
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            dry_run=result.dry_run,
            platform=result.platform,
            fetched=result.fetched,
            already_ingested=result.already_ingested,
            evaluated=result.evaluated,
            retained=result.retained,
            discarded=result.discarded,
            embedded=result.embedded,
            persisted=result.persisted,
            users_provisioned=result.users_provisioned,
            filter_errors=result.filter_errors,
            filter_provider=result.filter_provider,
            embedding_model=result.embedding_model,
            status=_status_of(result),
        )
        entity.decisions = [
            IngestionRunDecision(
                external_message_id=decision.id,
                keep=decision.keep,
                category=decision.category,
                reason=decision.reason,
                is_fallback=decision.is_fallback,
            )
            for decision in result.decisions
        ]

        try:
            async with self.uow.transaction():
                await self.uow.runs.add(entity)
        except Exception:
            logger.exception("could not record the ingestion run", extra={"run_id": result.run_id})

    async def history(
        self, *, limit: int = 20, platform: Platform | None = None
    ) -> Sequence[IngestionRun]:
        """The most recent runs, newest first, optionally scoped to one platform."""
        require_console_access(self.principal)
        return await self.uow.runs.list_recent(limit=limit, platform=platform)

    async def connectors(self) -> list[ConnectorHealth]:
        """One health row per platform, inferred from what has actually arrived.

        Three aggregate queries - accounts per platform, messages per platform,
        and the latest message per platform - joined in Python into one row per
        platform the system knows about.

        Every platform appears, including ones with nothing on them, because a
        platform silently missing from an integrations screen is indistinguishable
        from one that is working.
        """
        require_console_access(self.principal)
        accounts = await self.uow.relations.counts_by_platform()
        messages = await self.uow.messages.counts_by_platform()
        last_seen = await self.uow.messages.last_sent_by_platform()
        now = datetime.now(UTC)

        return [
            ConnectorHealth(
                platform=platform,
                status=_health_of(accounts.get(platform, 0), last_seen.get(platform), now),
                last_sync_at=last_seen.get(platform),
                messages_contributed=messages.get(platform, 0),
                account_count=accounts.get(platform, 0),
            )
            for platform in Platform
        ]


#: A run that hit no filtering fallbacks succeeded; one that hit some but still
#: stored messages was partial; one that stored nothing while trying to is
#: failed. Judged from the counters rather than from an exception, so a run that
#: completed badly is still distinguishable from one that completed well.
def _status_of(result: IngestionRunResult) -> str:
    """Reduce a run's counters to one word for the history list."""
    if result.filter_errors and result.persisted == 0 and result.retained:
        return "failed"
    if result.filter_errors:
        return "partial"
    return "success"


#: How stale a platform's traffic may be before the integrations screen says so.
CONNECTED_WITHIN = timedelta(days=7)
DEGRADED_WITHIN = timedelta(days=30)


def _health_of(account_count: int, last_seen: datetime | None, now: datetime) -> ConnectorStatus:
    """Infer a platform's status from its account count and last delivery."""
    if account_count == 0:
        return ConnectorStatus.DISCONNECTED
    if last_seen is None:
        return ConnectorStatus.NEEDS_ATTENTION
    age = now - last_seen
    if age <= CONNECTED_WITHIN:
        return ConnectorStatus.CONNECTED
    if age <= DEGRADED_WITHIN:
        return ConnectorStatus.DEGRADED
    return ConnectorStatus.NEEDS_ATTENTION
