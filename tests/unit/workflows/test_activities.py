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
