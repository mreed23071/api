"""Replay safety for the `ingestion-v2` patch.

The binding constraint on `workflows/ingestion.py` is that Temporal reconstructs
a workflow's state by running the code again from the top against the recorded
history. Change the sequence of commands the code issues and a workflow that was
mid-flight across the deploy fails with a non-determinism error - which is not a
crash you can retry your way out of, because the history is fixed.

So this file does the only thing that actually proves the patch works: it records
a history against the *pre-change* workflow body, then replays that history
against the current code and asserts Temporal accepts it.

`LegacyIngestionWorkflow` below is the previous body, kept verbatim as a fixture.
It is not dead code to be tidied up - it is the thing under test. It may be
deleted only when `workflow.deprecate_patch("ingestion-v2")` has been through a
full deploy cycle and no pre-patch workflow can still be in flight.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from app.domains.identity.models import Platform
from app.domains.ingestion.dto import FilterDecision, RawMessage
from app.workflows.config import (
    FETCH_RETRY,
    FETCH_TIMEOUT,
    INFERENCE_HEARTBEAT_TIMEOUT,
    INFERENCE_RETRY,
    INFERENCE_TIMEOUT,
    INGESTION_TASK_QUEUE,
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
from app.workflows.ingestion import IngestionWorkflow

pytestmark = pytest.mark.anyio

#: The constants as they stood before the sizes moved into the payload. The
#: fixture must chunk the way the old code did, not the way the new defaults do
#: - though they are deliberately equal, which is itself part of the contract.
LEGACY_FILTER_BATCH = 5
LEGACY_EMBED_BATCH = 16
LEGACY_PERSIST_BATCH = 50


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def message(index: int) -> RawMessage:
    return RawMessage(
        external_message_id=f"m{index}",
        platform=Platform.SLACK,
        external_author_id="U-ALICE",
        content="keep me",
        sent_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


def _chunks(items: list[RawMessage], size: int) -> list[list[RawMessage]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


@workflow.defn(name="IngestionWorkflow")
class LegacyIngestionWorkflow:
    """The workflow exactly as it was before `ingestion-v2`.

    Registered under the same workflow type name, so a history it records is
    indistinguishable from one the real deployment produced.
    """

    def __init__(self) -> None:
        self._progress = RunProgress()

    @workflow.query
    def progress(self) -> RunProgress:
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

        self._progress.stage = "fetching"
        fetched: FetchOutcome = await workflow.execute_activity(
            "fetch_candidates",
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

        self._progress.stage = "filtering"
        decisions: list[FilterDecision] = []
        for batch in _chunks(fetched.candidates, LEGACY_FILTER_BATCH):
            judged: FilterOutcome = await workflow.execute_activity(
                "filter_batch",
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
            m
            for m in fetched.candidates
            if m.external_message_id in verdicts and verdicts[m.external_message_id].keep
        ]
        summary.decisions = decisions
        summary.retained = len(retained)
        summary.discarded = summary.evaluated - summary.retained
        summary.filter_errors = sum(1 for d in decisions if d.is_fallback)

        self._progress.stage = "embedding"
        vectors: list[list[float]] = []
        embedding_model = ""
        for batch in _chunks(retained, LEGACY_EMBED_BATCH):
            embedded: EmbedOutcome = await workflow.execute_activity(
                "embed_batch",
                [m.content for m in batch],
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

        self._progress.stage = "persisting"
        persisted_total = 0
        users_total = 0
        relations_total = 0
        for start in range(0, len(retained), LEGACY_PERSIST_BATCH):
            batch = retained[start : start + LEGACY_PERSIST_BATCH]
            batch_vectors = vectors[start : start + LEGACY_PERSIST_BATCH]
            written: PersistOutcome = await workflow.execute_activity(
                "persist",
                PersistInput(
                    retained=batch,
                    vectors=batch_vectors,
                    # The whole run's decisions, as the old code sent them.
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

    async def _finish(self, summary: RunSummary, started_at) -> RunSummary:  # type: ignore[no-untyped-def]
        finished = workflow.now()
        summary.finished_at = finished
        summary.duration_ms = int((finished - started_at) / timedelta(milliseconds=1))
        await workflow.execute_activity(
            "record_run",
            summary,
            start_to_close_timeout=WRITE_TIMEOUT,
            retry_policy=WRITE_RETRY,
        )
        self._progress.stage = "done"
        return summary


def stub_activities():  # type: ignore[no-untyped-def]
    @activity.defn(name="fetch_candidates")
    async def fetch_candidates(payload: IngestionInput) -> FetchOutcome:
        candidates = [message(i) for i in range(12)]
        return FetchOutcome(
            candidates=candidates,
            fetched=len(candidates),
            filter_provider="stub",
            filter_prompt_version="v9",
        )

    @activity.defn(name="filter_batch")
    async def filter_batch(batch: list[RawMessage]) -> FilterOutcome:
        return FilterOutcome(
            decisions=[
                FilterDecision(id=m.external_message_id, keep=True, category="business")
                for m in batch
            ]
        )

    @activity.defn(name="embed_batch")
    async def embed_batch(texts: list[str]) -> EmbedOutcome:
        return EmbedOutcome(vectors=[[0.1] * 4 for _ in texts], model="nomic-embed-text")

    @activity.defn(name="persist")
    async def persist(payload: PersistInput) -> PersistOutcome:
        return PersistOutcome(
            persisted=len(payload.retained),
            newly_inserted=len(payload.retained),
            users_provisioned=1,
            relations_provisioned=1,
        )

    @activity.defn(name="record_run")
    async def record_run(summary: RunSummary) -> None:
        return None

    return [fetch_candidates, filter_batch, embed_batch, persist, record_run]


async def _record_legacy_history():  # type: ignore[no-untyped-def]
    """Run the pre-change workflow to completion and return its history."""
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        client: Client = env.client
        async with Worker(
            client,
            task_queue=INGESTION_TASK_QUEUE,
            workflows=[LegacyIngestionWorkflow],
            activities=stub_activities(),
            # The fixture workflow lives in a test module, which the sandbox
            # cannot re-import cleanly. It exists only to *produce* a history,
            # and the history is a wire artifact - nothing about how it was
            # recorded reaches the replay below, which runs the real workflow
            # under the real sandboxed runner.
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await client.start_workflow(
                LegacyIngestionWorkflow.run,
                # Deliberately constructed without the three batch fields, the
                # way a payload serialized before they existed would be.
                IngestionInput(run_id=str(uuid.uuid4()), platform=Platform.SLACK),
                id=f"legacy-{uuid.uuid4()}",
                task_queue=INGESTION_TASK_QUEUE,
            )
            await handle.result()
            return await handle.fetch_history()


async def test_a_pre_patch_history_replays_against_the_patched_workflow() -> None:
    """Acceptance check W3a, and the reason `_run_v1` exists.

    A history with no `ingestion-v2` marker must take the legacy branch, whose
    body is the old one verbatim - so the command sequence the replay produces
    matches the one in the history, event for event. Any drift here surfaces as
    `NondeterminismError`.
    """
    history = await _record_legacy_history()

    replayer = Replayer(
        workflows=[IngestionWorkflow],
        data_converter=pydantic_data_converter,
    )
    # Raises on any non-determinism; there is nothing to assert beyond it
    # returning, which is exactly the property under test.
    await replayer.replay_workflow(history)


def test_a_pre_patch_payload_deserializes_to_the_old_batch_sizes() -> None:
    """The other half of W3a: the defaults are not merely sensible, they are the
    old constants.

    A workflow started before the batch fields existed has an input in history
    that simply does not contain them - the JSON below is that shape. It has to
    revive with exactly the sizes it was recorded under, or the run chunks
    differently on replay than it did on its first execution, which is a
    non-determinism error. Asserted against the wire format rather than against
    a constructed object, because constructing one today would serialize the new
    fields and prove nothing.
    """
    from app.workflows.ingestion import EMBED_BATCH, FILTER_BATCH, PERSIST_BATCH

    recorded_before_the_change = (
        '{"run_id":"1bd19a98-c6fe-40b2-bf64-5cd17431d04b","platform":"slack",'
        '"limit":null,"system_prompt_override":null,"dry_run":false,'
        '"requested_by":"unknown"}'
    )

    revived = IngestionInput.model_validate_json(recorded_before_the_change)

    assert (revived.filter_batch, revived.embed_batch, revived.persist_batch) == (
        LEGACY_FILTER_BATCH,
        LEGACY_EMBED_BATCH,
        LEGACY_PERSIST_BATCH,
    )
    # And the constants the API injects still equal those defaults, so a run
    # started today chunks identically to one started before the change.
    assert (FILTER_BATCH, EMBED_BATCH, PERSIST_BATCH) == (
        LEGACY_FILTER_BATCH,
        LEGACY_EMBED_BATCH,
        LEGACY_PERSIST_BATCH,
    )
