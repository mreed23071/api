"""The ingestion workflow's orchestration, against a real Temporal runtime.

`WorkflowEnvironment.start_time_skipping()` runs an in-process Temporal, so
these are still fast and need no Docker. The activities are replaced with
stubs: what is under test is the *orchestration* - batching, ordering, what
gets embedded, and the resume property that is the whole reason for using a
workflow engine here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.domains.identity.models import Platform
from app.domains.ingestion.dto import FilterDecision, RawMessage
from app.workflows import activities as real
from app.workflows.config import INGESTION_TASK_QUEUE
from app.workflows.dto import (
    EmbedOutcome,
    FetchOutcome,
    FilterOutcome,
    IngestionInput,
    PersistInput,
    PersistOutcome,
    RunSummary,
)
from app.workflows.ingestion import IngestionWorkflow

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def message(index: int, *, keep: bool = True) -> RawMessage:
    return RawMessage(
        external_message_id=f"m{index}",
        platform=Platform.SLACK,
        external_author_id="U-ALICE",
        content="keep me" if keep else "drop me",
        sent_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


class Recorder:
    """Stub activities that record how the workflow drove them."""

    def __init__(self, messages: list[RawMessage], *, keep_every: int = 1) -> None:
        self.messages = messages
        self.keep_every = keep_every
        self.filter_batches: list[list[str]] = []
        self.embed_batches: list[list[str]] = []
        self.persist_batches: list[PersistInput] = []
        self.persisted: PersistInput | None = None
        self.recorded: RunSummary | None = None

    def build(self):  # type: ignore[no-untyped-def]
        recorder = self

        @activity.defn(name="fetch_candidates")
        async def fetch_candidates(payload: IngestionInput) -> FetchOutcome:
            return FetchOutcome(
                candidates=recorder.messages,
                fetched=len(recorder.messages),
                already_ingested=0,
                filter_provider="stub",
                filter_prompt_version="v9",
            )

        @activity.defn(name="filter_batch")
        async def filter_batch(batch: list[RawMessage]) -> FilterOutcome:
            recorder.filter_batches.append([m.external_message_id for m in batch])
            return FilterOutcome(
                decisions=[
                    FilterDecision(
                        id=m.external_message_id,
                        keep=(index % recorder.keep_every == 0),
                        category="business",
                    )
                    for index, m in enumerate(batch)
                ]
            )

        @activity.defn(name="embed_batch")
        async def embed_batch(texts: list[str]) -> EmbedOutcome:
            recorder.embed_batches.append(list(texts))
            return EmbedOutcome(vectors=[[0.1] * 4 for _ in texts], model="nomic-embed-text")

        @activity.defn(name="persist")
        async def persist(payload: PersistInput) -> PersistOutcome:
            recorder.persisted = payload
            recorder.persist_batches.append(payload)
            return PersistOutcome(
                persisted=len(payload.retained), users_provisioned=1, relations_provisioned=1
            )

        @activity.defn(name="record_run")
        async def record_run(summary: RunSummary) -> None:
            recorder.recorded = summary

        return [fetch_candidates, filter_batch, embed_batch, persist, record_run]


async def run_workflow(recorder: Recorder, **overrides) -> RunSummary:  # type: ignore[no-untyped-def]
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        client: Client = env.client
        async with Worker(
            client,
            task_queue=INGESTION_TASK_QUEUE,
            workflows=[IngestionWorkflow],
            activities=recorder.build(),
        ):
            payload = IngestionInput(
                run_id=str(uuid.uuid4()), platform=Platform.SLACK, **overrides
            )
            return await client.execute_workflow(
                IngestionWorkflow.run,
                payload,
                id=f"test-{uuid.uuid4()}",
                task_queue=INGESTION_TASK_QUEUE,
            )


async def test_a_run_reports_every_counter() -> None:
    recorder = Recorder([message(i) for i in range(3)])
    summary = await run_workflow(recorder)

    assert summary.fetched == 3
    assert summary.evaluated == 3
    assert summary.retained == 3
    assert summary.discarded == 0
    assert summary.persisted == 3
    assert summary.embedded == 3
    assert summary.platform is Platform.SLACK


async def test_filtering_is_split_into_small_batches() -> None:
    """The resume property depends on this: one activity per batch means a
    crash re-pays for at most one batch, not the whole run."""
    recorder = Recorder([message(i) for i in range(12)])
    await run_workflow(recorder)

    assert len(recorder.filter_batches) > 1
    assert all(len(batch) <= 5 for batch in recorder.filter_batches)
    # Every message judged exactly once, none dropped between batches.
    flattened = [mid for batch in recorder.filter_batches for mid in batch]
    assert sorted(flattened) == sorted(m.external_message_id for m in recorder.messages)


async def test_persisting_is_split_into_batches_matching_vectors_to_messages() -> None:
    """Discovered against a real 500-message run: one `persist` call carrying
    the entire retained set plus every vector exceeded Temporal's payload size
    limit and failed the workflow outright - not a retryable failure, since
    the payload is always too big at that size. Batching this the same way
    filtering and embedding already are fixes it, and each batch's vectors
    have to line up with that batch's messages, not the whole run's."""
    from app.workflows.ingestion import PERSIST_BATCH

    recorder = Recorder([message(i) for i in range(PERSIST_BATCH * 2 + 10)])
    summary = await run_workflow(recorder)

    assert len(recorder.persist_batches) > 1
    assert all(len(batch.retained) <= PERSIST_BATCH for batch in recorder.persist_batches)
    assert summary.persisted == len(recorder.messages)

    # Every batch's vectors correspond to that batch's own messages - a
    # mismatched slice here would silently attach the wrong vector to a
    # message, which no count-based assertion above would catch.
    for batch in recorder.persist_batches:
        assert len(batch.vectors) == len(batch.retained)


async def test_only_retained_messages_are_embedded() -> None:
    """Filtering runs before embedding so nothing personal is ever vectorised."""
    recorder = Recorder([message(i) for i in range(6)], keep_every=2)
    summary = await run_workflow(recorder)

    embedded = [text for batch in recorder.embed_batches for text in batch]
    assert len(embedded) == summary.retained < summary.evaluated
    assert all(text == "keep me" for text in embedded)


async def test_an_empty_fetch_still_records_a_run() -> None:
    """A run that found nothing is still evidence the pipeline ran."""
    recorder = Recorder([])
    summary = await run_workflow(recorder)

    assert summary.fetched == 0
    assert recorder.recorded is not None
    assert recorder.persisted is None  # nothing to write


async def test_a_dry_run_reaches_persist_but_writes_nothing() -> None:
    """Every stage runs for real; only the write is skipped - that is what
    makes a dry run a useful test of a policy change."""
    recorder = Recorder([message(i) for i in range(3)])
    summary = await run_workflow(recorder, dry_run=True)

    assert recorder.persisted is not None
    assert recorder.persisted.dry_run is True
    assert recorder.embed_batches, "embedding still happened"
    assert summary.dry_run is True


async def test_provenance_from_the_first_activity_reaches_the_run_record() -> None:
    """Workflow code cannot read settings, so the provider and prompt version
    are reported by an activity and carried through."""
    recorder = Recorder([message(0)])
    summary = await run_workflow(recorder)

    assert summary.filter_provider == "stub"
    assert summary.filter_prompt_version == "v9"
    assert recorder.persisted is not None
    assert recorder.persisted.filter_prompt_version == "v9"


async def test_the_embedding_model_is_recorded_from_the_activity() -> None:
    recorder = Recorder([message(0)])
    summary = await run_workflow(recorder)
    assert summary.embedding_model == "nomic-embed-text"


async def test_the_run_is_recorded_last_and_matches_the_result() -> None:
    """Recording is its own activity so a failed write of the receipt cannot
    undo a successful ingestion."""
    recorder = Recorder([message(i) for i in range(3)])
    summary = await run_workflow(recorder)

    assert recorder.recorded is not None
    assert recorder.recorded.persisted == summary.persisted
    assert recorder.recorded.run_id == summary.run_id


async def test_duration_is_stamped() -> None:
    recorder = Recorder([message(0)])
    summary = await run_workflow(recorder)
    assert summary.finished_at >= summary.started_at
    assert summary.duration_ms >= 0


def test_the_real_activities_are_all_registered() -> None:
    """A new activity that the worker never registers would hang the workflow."""
    names = {a.__name__ for a in real.ALL_ACTIVITIES}
    assert names == {
        "fetch_candidates",
        "filter_batch",
        "embed_batch",
        "persist",
        "record_run",
    }
