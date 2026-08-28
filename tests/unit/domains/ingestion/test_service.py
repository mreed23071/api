"""The ingestion pipeline, end to end, with every port faked.

Fast enough to run on every save, and it covers the branching that actually
breaks: dedupe, fail-closed filtering, provisioning, dry run, idempotency.
"""

from __future__ import annotations

import pytest

from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security.principal import Principal, Scope
from app.domains.ingestion.dto import IngestionOptions
from app.domains.ingestion.service import IngestionService
from app.domains.ingestion.sources import MockMessageService
from app.shared.llm.stub import StubLLMClient
from tests.conftest import make_principal
from tests.factories import make_raw_message
from tests.fakes import FailingLLMClient, FakeEmbeddingService, FakeUnitOfWork
from tests.fakes.sources import ScriptedMessageSource


def build(uow=None, source=None, llm=None, principal=None, settings=None):  # type: ignore[no-untyped-def]
    from app.core.config import get_settings

    embeddings = FakeEmbeddingService()
    embeddings.start()
    return IngestionService(
        uow=uow or FakeUnitOfWork(),
        principal=principal or make_principal(Scope.INGEST_RUN, Scope.INGEST_READ),
        source=source or MockMessageService(),
        llm=llm or StubLLMClient(),
        embeddings=embeddings,
        settings=settings or get_settings(),
    )


async def test_a_run_filters_embeds_and_persists() -> None:
    uow = FakeUnitOfWork()
    result = await build(uow).run(IngestionOptions())

    assert result.fetched == 12
    assert result.evaluated == 12
    assert result.retained + result.discarded == result.evaluated
    assert result.retained > 0
    assert result.embedded == result.retained
    assert result.persisted == result.retained
    assert len(uow.messages.messages) == result.retained


async def test_every_stored_message_carries_an_embedding_and_provenance() -> None:
    uow = FakeUnitOfWork()
    await build(uow).run(IngestionOptions())

    for stored in uow.messages.upserted:
        assert stored.embedding is not None and len(stored.embedding) == 384
        assert stored.embedding_model == "fake-embedder-v1"
        assert stored.filter_prompt_version
        assert stored.sender_user_id and stored.sender_relation_id


async def test_unknown_identities_are_provisioned_once() -> None:
    """Three fictional authors, each seen on more than one platform.

    A relation is created per (person, platform) pair that survived filtering -
    not per platform in the fixture - so the count is asserted as a range rather
    than a magic number that would break every time the fixture is edited.
    """
    uow = FakeUnitOfWork()
    result = await build(uow).run(IngestionOptions())

    assert result.users_provisioned == 3
    assert 3 <= result.relations_provisioned <= 9
    assert len({user.email for user in uow.users.users}) == 3
    assert len(uow.relations.relations) == result.relations_provisioned


async def test_a_person_seen_on_several_platforms_is_one_user() -> None:
    """The product's core claim, asserted against the real fixture."""
    uow = FakeUnitOfWork()
    await build(uow).run(IngestionOptions())

    platforms_per_user: dict = {}
    for relation in uow.relations.relations:
        platforms_per_user.setdefault(relation.user_id, set()).add(relation.platform)

    assert len(platforms_per_user) == 3
    assert any(len(platforms) > 1 for platforms in platforms_per_user.values())


async def test_a_second_run_writes_nothing_new() -> None:
    """The scheduler is at-least-once; a repeat must not duplicate rows."""
    uow = FakeUnitOfWork()
    first = await build(uow).run(IngestionOptions())
    second = await build(uow).run(IngestionOptions())

    assert second.already_ingested == first.persisted
    assert second.persisted == 0
    assert len(uow.messages.messages) == first.persisted


async def test_stored_messages_are_never_embedded_twice() -> None:
    """Dedupe runs before filtering and embedding, so a duplicate costs nothing."""
    uow = FakeUnitOfWork()
    first = await build(uow).run(IngestionOptions())

    second_embeddings = FakeEmbeddingService()
    second_embeddings.start()
    service = build(uow)
    service.embeddings = second_embeddings
    second = await service.run(IngestionOptions())

    assert second.already_ingested == first.persisted
    assert second.embedded == 0
    assert sum(len(batch) for batch in second_embeddings.calls) == 0


async def test_rejected_messages_are_re_evaluated_on_every_run() -> None:
    """A known limitation, pinned so the cost is visible rather than surprising.

    Dedupe consults *stored* messages only. Anything the filter rejects is never
    stored, so every subsequent run fetches and re-classifies it. With a real
    connector the per-run cost therefore grows with the total history of rejected
    messages. Tracked as R-5 in docs/PROTOTYPE-REPORT.md; the fix is a ledger of
    evaluated-and-rejected external ids.
    """
    uow = FakeUnitOfWork()
    first = await build(uow).run(IngestionOptions())
    second = await build(uow).run(IngestionOptions())

    assert first.discarded > 0
    assert second.evaluated == first.discarded
    assert second.persisted == 0


async def test_dry_run_rolls_everything_back() -> None:
    uow = FakeUnitOfWork()
    result = await build(uow).run(IngestionOptions(dry_run=True))

    assert result.dry_run is True
    assert result.retained > 0  # the pipeline really ran
    assert result.embedded > 0
    assert result.persisted == 0  # ...and nothing was kept
    assert result.users_provisioned == 0
    assert uow.rollbacks >= 1


async def test_limit_caps_the_source() -> None:
    result = await build().run(IngestionOptions(limit=4))
    assert result.fetched == 4


async def test_a_prompt_override_is_accepted_for_a_single_run() -> None:
    source = ScriptedMessageSource(
        [make_raw_message(external_message_id="m1", content="Deploy the release now.")]
    )
    result = await build(source=source).run(
        IngestionOptions(system_prompt_override="Keep nothing at all.", dry_run=True)
    )
    assert result.evaluated == 1


async def test_provider_outage_discards_and_reports_filter_errors() -> None:
    uow = FakeUnitOfWork()
    result = await build(uow, llm=FailingLLMClient()).run(IngestionOptions())

    assert result.retained == 0
    assert result.persisted == 0
    assert result.filter_errors == result.evaluated  # visible, not silent
    assert all(decision.is_fallback for decision in result.decisions)


async def test_discarded_messages_are_reconsidered_on_the_next_run() -> None:
    """Fail-closed must not mean permanently lost."""
    uow = FakeUnitOfWork()
    outage = await build(uow, llm=FailingLLMClient()).run(IngestionOptions())
    recovered = await build(uow).run(IngestionOptions())

    assert outage.persisted == 0
    assert recovered.evaluated == outage.evaluated
    assert recovered.persisted > 0


async def test_writes_happen_in_two_short_transactions() -> None:
    """One for provisioning + the message insert, one for the run record.

    Deliberately two, not one - see `record()`'s docstring in service.py: a
    dry run rolls its write transaction back, and sharing one transaction
    would take the history record down with it.
    """
    uow = FakeUnitOfWork()
    await build(uow).run(IngestionOptions())
    assert uow.transactions == 2


async def test_anonymous_callers_are_rejected_before_any_work() -> None:
    source = ScriptedMessageSource([make_raw_message()])
    with pytest.raises(AuthenticationError):
        await build(source=source, principal=Principal.anonymous()).run(IngestionOptions())
    assert source.fetch_calls == 0


async def test_a_reader_cannot_trigger_ingestion() -> None:
    with pytest.raises(AuthorizationError):
        await build(principal=make_principal(Scope.INSIGHTS_READ)).run(IngestionOptions())
