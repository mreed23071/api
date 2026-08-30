"""The worker's side-effecting half.

The two activities that only touch collaborators - filtering and embedding -
are exercised here with fakes. The three that open a database session
(`fetch_candidates`, `persist`, `record_run`) are covered by
`tests/integration/test_ingestion_pipeline.py`, which has a real Postgres;
faking a session here would test the fake.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.core.security.principal import Scope
from app.domains.identity.models import Platform
from app.domains.ingestion.dto import RawMessage
from app.shared.llm.stub import StubLLMClient
from app.workflows.activities import Deps, _status_of, embed_batch, filter_batch, worker_principal
from app.workflows.dto import RunSummary
from tests.fakes import FailingLLMClient, FakeEmbeddingService


@pytest.fixture
def deps(monkeypatch):  # type: ignore[no-untyped-def]
    """Replace the worker's process-wide collaborators with fakes."""
    embeddings = FakeEmbeddingService()
    embeddings.start()
    built = Deps(settings=get_settings(), llm=StubLLMClient(), embeddings=embeddings)
    monkeypatch.setattr("app.workflows.activities.get_deps", lambda: built)
    return built


def message(index: int, text: str = "The production deploy is blocked on review.") -> RawMessage:
    return RawMessage(
        external_message_id=f"m{index}",
        platform=Platform.SLACK,
        external_author_id="U-ALICE",
        content=text,
        sent_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


# -- the principal the worker acts as --------------------------------------


def test_the_worker_acts_as_itself_not_as_the_caller() -> None:
    """Scopes are checked at the edge; serializing a caller's credential into
    workflow history - which is durable and readable in the UI - would be a
    way to leak it."""
    principal = worker_principal()
    assert principal.subject == "ingestion-worker"
    assert Scope.INGEST_RUN in principal.scopes


def test_the_worker_has_no_more_scope_than_it_needs() -> None:
    assert principal_scopes() == {Scope.INGEST_RUN, Scope.INGEST_READ}


def principal_scopes() -> set[Scope]:
    return set(worker_principal().scopes)


# -- filtering --------------------------------------------------------------


async def test_filtering_returns_one_decision_per_message(deps) -> None:
    outcome = await filter_batch([message(0), message(1)])
    assert [d.id for d in outcome.decisions] == ["m0", "m1"]


async def test_a_provider_outage_fails_closed_and_is_flagged(deps, monkeypatch) -> None:
    """The activity must not raise: a batch that cannot be judged becomes
    fail-closed decisions, so the run reports the outage instead of dying."""
    monkeypatch.setattr(
        "app.workflows.activities.get_deps",
        lambda: Deps(settings=deps.settings, llm=FailingLLMClient(), embeddings=deps.embeddings),
    )
    outcome = await filter_batch([message(0)])

    assert outcome.decisions[0].keep is False
    assert outcome.decisions[0].is_fallback is True


# -- embedding --------------------------------------------------------------


async def test_embedding_preserves_order_and_reports_its_model(deps) -> None:
    outcome = await embed_batch(["one", "two", "three"])

    assert len(outcome.vectors) == 3
    assert outcome.model == "fake-embedder-v1"
    # Same text, same vector - the fake is deterministic, so a reordering here
    # would be visible.
    again = await embed_batch(["one"])
    assert again.vectors[0] == outcome.vectors[0]


async def test_embedding_an_empty_batch_is_harmless(deps) -> None:
    assert (await embed_batch([])).vectors == []


# -- run status -------------------------------------------------------------


def summary(**overrides) -> RunSummary:  # type: ignore[no-untyped-def]
    base = {
        "run_id": "r1",
        "platform": Platform.SLACK,
        "started_at": datetime(2026, 8, 28, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 28, tzinfo=UTC),
    }
    return RunSummary(**{**base, **overrides})


def test_a_clean_run_is_a_success() -> None:
    assert _status_of(summary(retained=5, persisted=5)) == "success"


def test_fail_closed_defaults_make_a_run_partial() -> None:
    """The policy was not really applied to those messages."""
    assert _status_of(summary(retained=5, persisted=5, filter_errors=2)) == "partial"


def test_a_run_that_kept_nothing_it_meant_to_keep_failed() -> None:
    assert _status_of(summary(retained=5, persisted=0, filter_errors=5)) == "failed"


# -- H4: a short embedding batch is caught at the activity layer -------------
#
# `embed_batch`'s contract is positional: vector i belongs to text i, and the
# workflow zips them back onto messages by index. An embedder that returns fewer
# vectors than texts silently shifts every subsequent message onto another
# message's vector - well-formed rows carrying wrong data, which no downstream
# constraint can detect.


class ShortEmbeddingService(FakeEmbeddingService):
    """Returns one fewer vector than asked for - a truncated provider response."""

    async def embed(self, texts):  # type: ignore[no-untyped-def]
        vectors = await super().embed(texts)
        return vectors[:-1]


async def test_a_short_embedding_batch_raises_rather_than_returning(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Acceptance check H4, activity layer."""
    from temporalio.exceptions import ApplicationError

    embeddings = ShortEmbeddingService()
    embeddings.start()
    built = Deps(settings=get_settings(), llm=StubLLMClient(), embeddings=embeddings)
    monkeypatch.setattr("app.workflows.activities.get_deps", lambda: built)

    with pytest.raises(ApplicationError) as caught:
        await embed_batch(["one", "two", "three"])

    assert caught.value.type == "ShortEmbeddingBatch"
    assert "2 vectors for 3 texts" in str(caught.value)


async def test_the_short_batch_error_is_retryable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A truncated response from a loaded Ollama is transient, and
    INFERENCE_RETRY exists for exactly that. The non-retryable backstop lives at
    the workflow layer, where reaching it means the retries were already spent.
    """
    from temporalio.exceptions import ApplicationError

    embeddings = ShortEmbeddingService()
    embeddings.start()
    built = Deps(settings=get_settings(), llm=StubLLMClient(), embeddings=embeddings)
    monkeypatch.setattr("app.workflows.activities.get_deps", lambda: built)

    with pytest.raises(ApplicationError) as caught:
        await embed_batch(["one", "two"])

    assert caught.value.non_retryable is False


async def test_a_correctly_sized_batch_passes_through(deps) -> None:  # type: ignore[no-untyped-def]
    """The guard must not fire on the healthy path."""
    outcome = await embed_batch(["one", "two", "three"])
    assert len(outcome.vectors) == 3


# -- H7: a cancelled in-flight call is unwound, not abandoned ----------------


async def test_cancelling_the_wrapper_unwinds_the_inner_call() -> None:
    """`_with_heartbeat` used to call `work.cancel()` and return immediately.

    Cancelling only *requests* cancellation - the coroutine has not unwound
    until it is awaited. Abandoning it meant httpx never released its connection
    back to the transport pool, and the loop printed "Task was destroyed but it
    is pending!" on every worker shutdown.
    """
    import asyncio

    from app.workflows.activities import _with_heartbeat

    unwound = asyncio.Event()

    async def slow_call() -> str:
        try:
            await asyncio.sleep(30)
            return "never"
        except asyncio.CancelledError:
            # Stands in for httpx returning its connection to the pool.
            unwound.set()
            raise

    task = asyncio.create_task(_with_heartbeat(slow_call()))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert unwound.is_set(), "the in-flight call was abandoned rather than unwound"


async def test_an_error_raised_while_cancelling_does_not_escape() -> None:
    """Teardown noise must not replace the cancellation being propagated.

    The outer CancelledError is what tells Temporal the activity was cancelled;
    an exception thrown by the inner call on its way out is logged, not raised.
    """
    import asyncio

    from app.workflows.activities import _with_heartbeat

    async def badly_behaved() -> str:
        try:
            await asyncio.sleep(30)
            return "never"
        except asyncio.CancelledError:
            raise RuntimeError("cleanup exploded") from None

    task = asyncio.create_task(_with_heartbeat(badly_behaved()))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_wrapper_returns_a_completed_result_untouched() -> None:
    """The happy path is unchanged; only the cancellation path moved."""
    import asyncio

    from app.workflows.activities import _with_heartbeat

    async def quick() -> str:
        await asyncio.sleep(0)
        return "done"

    assert await _with_heartbeat(quick()) == "done"
