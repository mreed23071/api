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
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
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


def failure_chain(error: BaseException) -> str:
    """Every message in a workflow failure's cause chain, joined.

    `WorkflowFailureError` says only "Workflow execution failed"; what actually
    went wrong is on `__cause__`, wrapped once per boundary it crossed.
    """
    messages: list[str] = []
    current: BaseException | None = error
    while current is not None:
        messages.append(str(current))
        current = current.__cause__
    return " | ".join(messages)


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
            payload = IngestionInput(run_id=str(uuid.uuid4()), platform=Platform.SLACK, **overrides)
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


def test_every_activity_the_workflow_names_is_registered() -> None:
    """The check that makes the string-name invocation safe.

    The workflow no longer imports `activities` - it names each one by its
    registered string, which is what keeps the database engine and the model
    factories out of the workflow's module graph. The cost of that is a
    reference the type checker cannot follow: rename an activity function and
    the workflow keeps compiling, then hangs at runtime waiting for a task
    nobody will ever poll for. This closes that gap.
    """
    from app.workflows import ingestion as workflow_module

    referenced = {
        workflow_module.FETCH_CANDIDATES,
        workflow_module.FILTER_BATCH_ACTIVITY,
        workflow_module.EMBED_BATCH_ACTIVITY,
        workflow_module.PERSIST,
        workflow_module.RECORD_RUN,
    }
    registered = {a.__name__ for a in real.ALL_ACTIVITIES}

    orphaned = referenced - registered
    assert not orphaned, (
        f"The workflow names activities that no worker registers: {sorted(orphaned)}. "
        "A workflow that calls one of these waits forever rather than failing."
    )


def test_the_workflow_does_not_import_the_activities_module() -> None:
    """The sandbox property B exists for, asserted rather than described.

    Importing `app.workflows.activities` pulls in the database engine, the
    embedding factory and the LLM factory - so Temporal's sandbox had to pass
    all of it through unvalidated, and stopped being able to tell workflow code
    from side-effecting code. Scanning the source rather than the module graph
    keeps the assertion about what this file *declares*.
    """
    import ast
    from pathlib import Path

    source = Path(real.__file__).parent / "ingestion.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = {
        name
        for name in imported
        if name.endswith("workflows.activities")
        or name.endswith("core.db.engine")
        or name.endswith("embeddings.factory")
        or name.endswith("llm.factory")
    }
    assert not forbidden, f"ingestion.py must not import {sorted(forbidden)}"


def test_the_dtos_the_workflow_imports_do_not_drag_in_the_database() -> None:
    """The transitive half of the same property.

    `workflows/dto.py` imports the mapped models for their `Platform` enum, and
    those import `Base` - which used to pull `app.core.db.engine` in through
    `app/core/db/__init__.py`'s re-exports. Importing the workflow's own DTOs
    therefore constructed the settings singleton and the engine module.
    """
    import subprocess
    import sys

    probe = (
        "import sys, importlib;"
        "importlib.import_module('app.workflows.dto');"
        "importlib.import_module('app.domains.ingestion.dto');"
        "bad=[m for m in ("
        "'app.core.db.engine','app.shared.llm.factory','app.shared.embeddings.factory'"
        ") if m in sys.modules];"
        "print(','.join(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    leaked = result.stdout.strip()
    assert not leaked, f"importing the workflow DTOs also imported: {leaked}"


# -- H4: misaligned vectors never reach a write ------------------------------


async def test_a_short_embedding_batch_fails_the_run_before_anything_is_written() -> None:
    """The data-corruption gate.

    Stage 4 zips `retained` against `vectors` positionally. An embedder that
    returned fewer vectors than it was given texts used to shift every message
    from that batch onwards onto another message's vector - and because
    persisting is batched, the earlier batches committed *before* the run
    failed. Wrong vectors, durably stored, in a run reported as failed.

    Now the count is checked after the embed loop and before stage 4, so the run
    fails with nothing written at all.
    """
    recorder = ShortEmbeddingRecorder([message(i) for i in range(6)])

    with pytest.raises(WorkflowFailureError) as caught:
        await run_workflow(recorder)

    assert "misaligned" in failure_chain(caught.value)
    assert recorder.persist_batches == [], "nothing may be persisted once alignment is lost"


class ShortEmbeddingRecorder(Recorder):
    """Returns one fewer vector than it was asked for, on every batch."""

    def build(self):  # type: ignore[no-untyped-def]
        activities_list = super().build()
        recorder = self

        @activity.defn(name="embed_batch")
        async def embed_batch(texts: list[str]) -> EmbedOutcome:
            recorder.embed_batches.append(list(texts))
            # One short. The real activity raises a *retryable* error for this;
            # here we let it through to prove the workflow-layer backstop.
            return EmbedOutcome(vectors=[[0.1] * 4 for _ in texts[:-1]], model="nomic-embed-text")

        return [a for a in activities_list if a.__name__ != "embed_batch"] + [embed_batch]


# -- W2: a failed run leaves a database trace --------------------------------


class FailingFilterRecorder(Recorder):
    """Fails filtering non-retryably, the way an exhausted retry policy would."""

    def build(self):  # type: ignore[no-untyped-def]
        activities_list = super().build()
        recorder = self

        @activity.defn(name="filter_batch")
        async def filter_batch(batch: list[RawMessage]) -> FilterOutcome:
            recorder.filter_batches.append([m.external_message_id for m in batch])
            raise ApplicationError("the model is unreachable", non_retryable=True)

        return [a for a in activities_list if a.__name__ != "filter_batch"] + [filter_batch]


async def test_a_failed_run_still_records_itself() -> None:
    """A run that raises used to leave no row at all.

    `record_run` was only ever reached from `_finish`, on the success path - so
    the console queried `ingestion_runs`, found nothing, and showed the run as
    never having existed. The failures most worth seeing were the invisible
    ones.
    """
    recorder = FailingFilterRecorder([message(i) for i in range(3)])

    with pytest.raises(WorkflowFailureError):
        await run_workflow(recorder)

    assert recorder.recorded is not None, "the failed run recorded nothing"
    assert recorder.recorded.status_override == "failed"
    assert recorder.recorded.run_id
    assert recorder.recorded.duration_ms >= 0


async def test_recording_a_failure_does_not_mask_the_original_error() -> None:
    """The receipt is best effort; the failure it describes is not."""

    class RecordAlsoFails(FailingFilterRecorder):
        def build(self):  # type: ignore[no-untyped-def]
            activities_list = super().build()

            @activity.defn(name="record_run")
            async def record_run(summary: RunSummary) -> None:
                raise ApplicationError("the database is gone too", non_retryable=True)

            return [a for a in activities_list if a.__name__ != "record_run"] + [record_run]

    recorder = RecordAlsoFails([message(0)])

    with pytest.raises(WorkflowFailureError) as caught:
        await run_workflow(recorder)

    chain = failure_chain(caught.value)
    assert "unreachable" in chain, (
        f"the filter failure must survive; the recording failure is swallowed. Got: {chain}"
    )
    assert "database is gone" not in chain


# -- W3a: batch sizes travel in the payload ----------------------------------


async def test_batch_sizes_come_from_the_payload() -> None:
    """Retuning the module constants must not change a running workflow.

    The sizes are read from the recorded input, so an execution chunks the same
    way on replay as it did on its first run no matter what the constants say
    by then.
    """
    recorder = Recorder([message(i) for i in range(9)])
    await run_workflow(recorder, filter_batch=3, embed_batch=2, persist_batch=4)

    assert [len(b) for b in recorder.filter_batches] == [3, 3, 3]
    assert all(len(b) <= 2 for b in recorder.embed_batches)
    assert all(len(b.retained) <= 4 for b in recorder.persist_batches)


async def test_the_payload_defaults_match_the_module_constants() -> None:
    """What makes the new fields replay-safe for histories recorded without them.

    An old payload deserializes with these defaults, so it must chunk exactly
    the way the constants used to.
    """
    from app.workflows.ingestion import EMBED_BATCH, FILTER_BATCH, PERSIST_BATCH

    payload = IngestionInput(run_id=str(uuid.uuid4()), platform=Platform.SLACK)
    assert payload.filter_batch == FILTER_BATCH
    assert payload.embed_batch == EMBED_BATCH
    assert payload.persist_batch == PERSIST_BATCH


# -- payload-size waste: each batch gets only its own verdicts ---------------


async def test_each_persist_batch_carries_only_its_own_decisions() -> None:
    """The whole run's decision list used to be re-sent with every batch.

    For a 500-message run that meant all 500 verdicts, ten times over. `persist`
    builds its own id-keyed dict from whatever it is given, so a batch needs
    only the verdicts for the messages in it.
    """
    recorder = Recorder([message(i) for i in range(12)])
    await run_workflow(recorder, persist_batch=5)

    assert len(recorder.persist_batches) > 1
    for batch in recorder.persist_batches:
        assert len(batch.decisions) == len(batch.retained)
        sent = {d.id for d in batch.decisions}
        assert sent == {m.external_message_id for m in batch.retained}
