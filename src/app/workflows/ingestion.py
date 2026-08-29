"""The ingestion workflow.

This module is *replayed*. Temporal reconstructs a workflow's state by running
this code again from the top against the recorded history, so everything here
must be deterministic: no I/O, no `datetime.now()`, no `random`, no reading
settings. Every one of those lives in `activities.py`, which is only ever
executed once per attempt and whose results are recorded.

The shape is the same five steps the synchronous pipeline documents - fetch,
filter, embed, persist, record - with one difference that is the entire reason
for the change: filtering and embedding run as one activity *per batch*. A
worker that dies half way through a run resumes at the next unfinished batch
instead of re-running the model over everything it had already judged. With a
local 3B model that is minutes; against a hosted provider it is money.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.domains.ingestion.dto import FilterDecision, RawMessage
    from app.workflows import activities
    from app.workflows.config import (
        FETCH_RETRY,
        FETCH_TIMEOUT,
        INFERENCE_HEARTBEAT_TIMEOUT,
        INFERENCE_RETRY,
        INFERENCE_TIMEOUT,
        WRITE_RETRY,
        WRITE_TIMEOUT,
    )
    from app.workflows.dto import (
        FetchOutcome,
        IngestionInput,
        PersistInput,
        PersistOutcome,
        RunProgress,
        RunSummary,
    )

#: How many messages go to the model in one activity. Small on purpose: it
#: bounds how much work a crash can cost, and a local model classifies a short
#: list far more reliably than a long one.
FILTER_BATCH = 5
EMBED_BATCH = 16

#: How many messages (plus their embedding vectors) go into one `persist`
#: activity call. Discovered the hard way: Temporal caps a single activity's
#: scheduled input at 2MB, and a run's *entire* retained set plus every vector
#: used to go into one `persist` call. A 500-message run's payload measured
#: ~4MB - comfortably past the limit - and the workflow failed outright with
#: `BadScheduleActivityAttributes: ... Input exceeds size limit`, not a
#: retryable error, since the payload is always going to be too big at that
#: size no matter how many times it's retried. 50 messages' worth of content
#: plus a nomic-embed-text vector each lands well under Temporal's 512KB
#: *warning* threshold, with real margin before the 2MB hard limit.
PERSIST_BATCH = 50


@workflow.defn(name="IngestionWorkflow")
class IngestionWorkflow:
    """One ingestion run, per platform, durably."""

    def __init__(self) -> None:
        self._progress = RunProgress()

    @workflow.query
    def progress(self) -> RunProgress:
        """Live counters, for the console's run screen.

        A query runs against current workflow state without touching history,
        so the UI can poll this cheaply while a run is in flight.
        """
        return self._progress

    @workflow.run
    async def run(self, payload: IngestionInput) -> RunSummary:
        started_at = workflow.now()
        self._progress.platform = payload.platform

        summary = RunSummary(
            run_id=payload.run_id,
            platform=payload.platform,
            started_at=started_at,
            finished_at=started_at,
            dry_run=payload.dry_run,
        )

        # 1. Fetch, and drop what is already stored.
        self._progress.stage = "fetching"
        fetched: FetchOutcome = await workflow.execute_activity(
            activities.fetch_candidates,
            payload,
            start_to_close_timeout=FETCH_TIMEOUT,
            retry_policy=FETCH_RETRY,
        )
        summary.fetched = fetched.fetched
        summary.already_ingested = fetched.already_ingested
        summary.evaluated = len(fetched.candidates)
        summary.filter_provider = fetched.filter_provider
        summary.filter_prompt_version = fetched.filter_prompt_version
        self._progress.fetched = fetched.fetched
        self._progress.evaluated = summary.evaluated

        if not fetched.candidates:
            return await self._finish(summary, started_at)

        # 2. Filter, one activity per batch. Each batch's verdicts land in
        #    history, so a crash never re-pays for the ones already judged.
        self._progress.stage = "filtering"
        decisions: list[FilterDecision] = []
        for batch in _chunks(fetched.candidates, FILTER_BATCH):
            judged = await workflow.execute_activity(
                activities.filter_batch,
                batch,
                start_to_close_timeout=INFERENCE_TIMEOUT,
                heartbeat_timeout=INFERENCE_HEARTBEAT_TIMEOUT,
                retry_policy=INFERENCE_RETRY,
            )
            decisions.extend(judged.decisions)
            self._progress.filtered = len(decisions)

        verdicts = {decision.id: decision for decision in decisions}
        retained = [
            message
            for message in fetched.candidates
            if message.external_message_id in verdicts
            and verdicts[message.external_message_id].keep
        ]
        summary.decisions = decisions
        summary.retained = len(retained)
        summary.discarded = summary.evaluated - summary.retained
        summary.filter_errors = sum(1 for decision in decisions if decision.is_fallback)

        # 3. Embed only what survived, so nothing personal is ever vectorised.
        self._progress.stage = "embedding"
        vectors: list[list[float]] = []
        embedding_model = ""
        for batch in _chunks(retained, EMBED_BATCH):
            embedded = await workflow.execute_activity(
                activities.embed_batch,
                [message.content for message in batch],
                start_to_close_timeout=INFERENCE_TIMEOUT,
                heartbeat_timeout=INFERENCE_HEARTBEAT_TIMEOUT,
                retry_policy=INFERENCE_RETRY,
            )
            vectors.extend(embedded.vectors)
            embedding_model = embedded.model or embedding_model
            self._progress.embedded = len(vectors)

        summary.embedded = len(vectors)
        summary.embedding_model = embedding_model

        # 4. Write, in batches - transactional and idempotent per batch.
        #
        # `decisions` (not chunked) is passed to every batch call in full: the
        # persist activity looks each message's verdict up by id, and the
        # decisions list alone - no message content, no vectors - stays small
        # enough to never approach the payload limit PERSIST_BATCH exists for.
        self._progress.stage = "persisting"
        persisted_total = 0
        users_total = 0
        relations_total = 0
        for start in range(0, len(retained), PERSIST_BATCH):
            batch = retained[start : start + PERSIST_BATCH]
            batch_vectors = vectors[start : start + PERSIST_BATCH]
            written: PersistOutcome = await workflow.execute_activity(
                activities.persist,
                PersistInput(
                    retained=batch,
                    vectors=batch_vectors,
                    decisions=decisions,
                    embedding_model=embedding_model,
                    filter_prompt_version=summary.filter_prompt_version,
                    dry_run=payload.dry_run,
                ),
                start_to_close_timeout=WRITE_TIMEOUT,
                retry_policy=WRITE_RETRY,
            )
            persisted_total += written.persisted
            users_total += written.users_provisioned
            relations_total += written.relations_provisioned
            self._progress.persisted = persisted_total

        summary.persisted = persisted_total
        summary.users_provisioned = users_total
        summary.relations_provisioned = relations_total

        return await self._finish(summary, started_at)

    async def _finish(self, summary: RunSummary, started_at) -> RunSummary:  # type: ignore[no-untyped-def]
        """Stamp timing and write the receipt.

        Recording is its own activity and deliberately last: the work has
        already happened, so a failure to write history must not undo it.
        """
        finished = workflow.now()
        summary.finished_at = finished
        summary.duration_ms = int((finished - started_at) / timedelta(milliseconds=1))

        await workflow.execute_activity(
            activities.record_run,
            summary,
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=WRITE_RETRY,
        )
        self._progress.stage = "done"
        return summary


def _chunks(items: list[RawMessage], size: int) -> list[list[RawMessage]]:
    """Deterministic, allocation-only - safe to call during replay."""
    return [items[start : start + size] for start in range(0, len(items), size)]
