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

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.domains.ingestion.dto import FilterDecision, RawMessage
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
        EmbedOutcome,
        FetchOutcome,
        FilterOutcome,
        IngestionInput,
        PersistInput,
        PersistOutcome,
        RunProgress,
        RunSummary,
    )

# `app.workflows.activities` is deliberately *not* imported. Importing it pulled
# the database engine, the embedding factory and the LLM factory into the
# workflow's module graph, which meant Temporal's sandbox had to pass all of it
# through unvalidated - the sandbox stopped being able to tell workflow code
# from side-effecting code. Activities are invoked by their registered string
# name instead, with an explicit `result_type` so the data converter still
# returns typed objects rather than dicts.
#
# The registered name of an activity equals its function name, so from the SDK's
# point of view nothing about these calls changed and the switch needs no patch
# gate. `tests/unit/workflows/test_ingestion_workflow.py` asserts every string
# below matches a registered activity, so a rename cannot silently orphan one.
FETCH_CANDIDATES = "fetch_candidates"
FILTER_BATCH_ACTIVITY = "filter_batch"
EMBED_BATCH_ACTIVITY = "embed_batch"
PERSIST = "persist"
RECORD_RUN = "record_run"

#: The one patch marker guarding every behavioural change in this file. Old
#: histories replay through `_run_v1`, which is the pre-change body verbatim;
#: new executions take `_run_v2`.
#:
#: TODO: once every workflow started before this deploy has completed, promote
#: this to `workflow.deprecate_patch("ingestion-v2")` and then delete the v1
#: branch. Do not do it in the same deploy - a workflow still mid-flight on the
#: old branch would fail to replay.
PATCH_ID = "ingestion-v2"

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
        """Dispatch to the version of the pipeline this execution was started on.

        The branch is structural rather than a scatter of inline `patched()`
        checks so that a legacy history replays against *byte-equivalent* logic:
        `_run_v1` is the previous body, untouched. Anything else risks a
        non-determinism error for a workflow that is merely old.
        """
        if workflow.patched(PATCH_ID):
            return await self._run_v2(payload)
        return await self._run_v1(payload)

    # -- v1: the pre-change body, verbatim. Do not edit. ---------------------
    #
    # Reached only by workflows started before the "ingestion-v2" patch was
    # deployed. Every change belongs in `_run_v2`.

    async def _run_v1(self, payload: IngestionInput) -> RunSummary:
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
            FETCH_CANDIDATES,
            payload,
            result_type=FetchOutcome,
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
            judged: FilterOutcome = await workflow.execute_activity(
                FILTER_BATCH_ACTIVITY,
                batch,
                result_type=FilterOutcome,
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
            embedded: EmbedOutcome = await workflow.execute_activity(
                EMBED_BATCH_ACTIVITY,
                [message.content for message in batch],
                result_type=EmbedOutcome,
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
                PERSIST,
                PersistInput(
                    retained=batch,
                    vectors=batch_vectors,
                    decisions=decisions,
                    embedding_model=embedding_model,
                    filter_prompt_version=summary.filter_prompt_version,
                    dry_run=payload.dry_run,
                ),
                result_type=PersistOutcome,
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

    # -- v2: the current pipeline -------------------------------------------

    async def _run_v2(self, payload: IngestionInput) -> RunSummary:
        """The same five stages, with a database trace when they fail.

        Differences from v1, all of them deliberate:

        * batch sizes come from the payload, so they are frozen per execution
          and retuning the module constants can never desynchronize a replay;
        * each persist batch is handed only its own verdicts, not the run's
          entire decision list;
        * the vector/message alignment is checked before anything is written;
        * a run that raises still records a `"failed"` row, so a failure is
          visible in the console rather than only in Temporal.
        """
        started_at = workflow.now()
        self._progress.platform = payload.platform

        # Frozen into this execution's recorded input at submission time. See
        # `IngestionInput` for why the constants can no longer be read here.
        filter_batch = payload.filter_batch
        embed_batch = payload.embed_batch
        persist_batch = payload.persist_batch

        summary = RunSummary(
            run_id=payload.run_id,
            platform=payload.platform,
            started_at=started_at,
            finished_at=started_at,
            dry_run=payload.dry_run,
        )

        try:
            # 1. Fetch, and drop what is already stored.
            self._progress.stage = "fetching"
            fetched: FetchOutcome = await workflow.execute_activity(
                FETCH_CANDIDATES,
                payload,
                result_type=FetchOutcome,
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
            for batch in _chunks(fetched.candidates, filter_batch):
                judged: FilterOutcome = await workflow.execute_activity(
                    FILTER_BATCH_ACTIVITY,
                    batch,
                    result_type=FilterOutcome,
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
            for batch in _chunks(retained, embed_batch):
                embedded: EmbedOutcome = await workflow.execute_activity(
                    EMBED_BATCH_ACTIVITY,
                    [message.content for message in batch],
                    result_type=EmbedOutcome,
                    start_to_close_timeout=INFERENCE_TIMEOUT,
                    heartbeat_timeout=INFERENCE_HEARTBEAT_TIMEOUT,
                    retry_policy=INFERENCE_RETRY,
                )
                vectors.extend(embedded.vectors)
                embedding_model = embedded.model or embedding_model
                self._progress.embedded = len(vectors)

            # The gate that makes misaligned data impossible to persist rather
            # than merely unlikely. Stage 4 zips `retained` against `vectors` by
            # index, so a count mismatch means every message from the short
            # batch onwards would be stored with another message's vector -
            # well-formed rows carrying silently wrong data, which nothing
            # downstream can detect. Non-retryable: if the counts disagree here
            # the activity's own retries have already been exhausted, and the
            # run must fail before a single row is written rather than commit
            # every batch but the last and then fail anyway.
            #
            # A pure check issues no commands, so it needs no patch gate.
            if len(vectors) != len(retained):
                raise ApplicationError(
                    f"embedding produced {len(vectors)} vectors for "
                    f"{len(retained)} retained messages; refusing to persist misaligned data",
                    type="EmbeddingAlignmentError",
                    non_retryable=True,
                )

            summary.embedded = len(vectors)
            summary.embedding_model = embedding_model

            # 4. Write, in batches - transactional and idempotent per batch.
            #
            # Each batch is handed only the verdicts for the messages in it.
            # v1 passed the run's entire decision list to every call, which for
            # a 500-message run meant re-sending all 500 verdicts ten times over.
            # `persist` builds its own id-keyed dict from whatever it receives,
            # so it needs no change.
            self._progress.stage = "persisting"
            persisted_total = 0
            users_total = 0
            relations_total = 0
            for start in range(0, len(retained), persist_batch):
                batch = retained[start : start + persist_batch]
                batch_vectors = vectors[start : start + persist_batch]
                batch_decisions = [verdicts[message.external_message_id] for message in batch]
                written: PersistOutcome = await workflow.execute_activity(
                    PERSIST,
                    PersistInput(
                        retained=batch,
                        vectors=batch_vectors,
                        decisions=batch_decisions,
                        embedding_model=embedding_model,
                        filter_prompt_version=summary.filter_prompt_version,
                        dry_run=payload.dry_run,
                    ),
                    result_type=PersistOutcome,
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

        except asyncio.CancelledError:
            # Cancellation is not failure. Re-raise before the recording path so
            # a cancelled run's semantics are exactly what they were.
            raise
        except Exception:
            # A failed run used to leave no database trace at all: the console
            # queried `ingestion_runs`, found nothing, and showed the run as
            # never having existed - the failures most worth seeing were the
            # ones that were invisible. Record a "failed" row, then let the
            # original exception continue so Temporal still marks the execution
            # failed.
            finished = workflow.now()
            summary.finished_at = finished
            summary.duration_ms = int((finished - started_at) / timedelta(milliseconds=1))
            summary.status_override = "failed"
            self._progress.stage = "failed"
            try:
                await workflow.execute_activity(
                    RECORD_RUN,
                    summary,
                    start_to_close_timeout=WRITE_TIMEOUT,
                    retry_policy=WRITE_RETRY,
                )
            except Exception:
                # Best effort by design. Failing to write the receipt must never
                # replace the real failure with a less informative one.
                pass
            raise

    async def _finish(self, summary: RunSummary, started_at) -> RunSummary:  # type: ignore[no-untyped-def]
        """Stamp timing and write the receipt.

        Recording is its own activity and deliberately last: the work has
        already happened, so a failure to write history must not undo it.
        """
        finished = workflow.now()
        summary.finished_at = finished
        summary.duration_ms = int((finished - started_at) / timedelta(milliseconds=1))

        await workflow.execute_activity(
            RECORD_RUN,
            summary,
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=WRITE_RETRY,
        )
        self._progress.stage = "done"
        return summary


def _chunks(items: list[RawMessage], size: int) -> list[list[RawMessage]]:
    """Deterministic, allocation-only - safe to call during replay."""
    return [items[start : start + size] for start in range(0, len(items), size)]
