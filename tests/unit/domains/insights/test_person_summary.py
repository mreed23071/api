"""Summarising one person, optionally over a slice of their history."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.core.errors import AuthenticationError, AuthorizationError, NotFoundError
from app.core.security.principal import Principal, Scope
from app.domains.insights.dto import SummaryWindow
from app.domains.insights.service import UserInsightsService
from app.shared.llm.stub import StubLLMClient
from tests.conftest import make_principal
from tests.factories import make_message, make_user
from tests.fakes.uow import FakeUnitOfWork

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def build(uow: FakeUnitOfWork, principal: Principal | None = None) -> UserInsightsService:
    return UserInsightsService(
        uow=uow,
        # This route returns the same class of personal data as the list
        # endpoint - names, verbatim messages, a behavioural narrative - and now
        # demands the same two scopes for it.
        principal=principal or make_principal(Scope.INSIGHTS_READ, Scope.MESSAGES_READ),
        llm=StubLLMClient(),
        settings=get_settings(),
    )


def person_with_history() -> tuple[UserInsightsService, FakeUnitOfWork, uuid.UUID]:
    amara = make_user()
    uow = FakeUnitOfWork(
        users=[amara],
        messages=[
            make_message(amara, sent_at=NOW),
            make_message(amara, sent_at=NOW - timedelta(days=10)),
            make_message(amara, sent_at=NOW - timedelta(days=100)),
        ],
    )
    return build(uow), uow, amara.id


async def test_an_unbounded_window_covers_everything() -> None:
    svc, _, amara_id = person_with_history()

    summary = await svc.summarize_person(amara_id)

    assert summary.window.is_unbounded
    assert summary.message_count == 3
    assert summary.summary and not summary.summary_error


async def test_a_window_narrows_what_the_summary_is_built_from() -> None:
    svc, _, amara_id = person_with_history()

    summary = await svc.summarize_person(amara_id, SummaryWindow(starting=NOW - timedelta(days=30)))

    assert summary.message_count == 2


async def test_recent_messages_are_capped_and_newest_first() -> None:
    svc, _, amara_id = person_with_history()

    summary = await svc.summarize_person(amara_id, recent=2)

    assert len(summary.recent_messages) == 2
    assert summary.recent_messages[0].sent_at == NOW


async def test_a_window_containing_nothing_explains_itself() -> None:
    """Not an error - there is simply nothing in that slice to describe."""
    svc, _, amara_id = person_with_history()

    summary = await svc.summarize_person(amara_id, SummaryWindow(starting=NOW + timedelta(days=1)))

    assert summary.message_count == 0
    assert summary.summary is None
    assert summary.summary_error


async def test_the_generation_records_which_model_produced_it() -> None:
    """Provenance: a summary is only interpretable if you know what wrote it."""
    svc, _, amara_id = person_with_history()

    summary = await svc.summarize_person(amara_id)

    assert summary.llm_provider and summary.llm_model


async def test_summarising_an_unknown_person_is_a_not_found() -> None:
    svc, _, _ = person_with_history()

    with pytest.raises(NotFoundError):
        await svc.summarize_person(uuid.uuid4())


# -- W4: the per-person summary carries the same scope demand as the list ----
#
# This method returned the list endpoint's exact PII class - real names, email
# addresses, verbatim message text, a generated behavioural narrative - with no
# scope check at all, while its sibling required two. These assert the gap
# stays closed at the service layer, independently of the route's dependency.


async def test_an_anonymous_caller_cannot_summarise_a_person() -> None:
    amara = make_user()
    svc = build(FakeUnitOfWork(users=[amara]), principal=Principal.anonymous())

    with pytest.raises(AuthenticationError):
        await svc.summarize_person(amara.id)


async def test_half_the_required_scopes_is_not_enough() -> None:
    """`insights:read` alone does not unlock verbatim message content."""
    amara = make_user()
    svc = build(FakeUnitOfWork(users=[amara]), principal=make_principal(Scope.INSIGHTS_READ))

    with pytest.raises(AuthorizationError):
        await svc.summarize_person(amara.id)


async def test_the_scope_check_precedes_the_lookup() -> None:
    """An unauthorised caller learns nothing, not even whether the person exists.

    The check is the method's first statement for this reason: running the
    lookup first would answer 404-vs-403 for an id the caller may not ask about.
    """
    svc = build(FakeUnitOfWork(users=[]), principal=Principal.anonymous())

    with pytest.raises(AuthenticationError):
        await svc.summarize_person(uuid.uuid4())


# -- W1: the connection is released before the model call --------------------


async def test_the_connection_is_released_before_the_model_runs() -> None:
    """The single-user half of the pool-exhaustion fix.

    One open person view held a pooled connection for the whole of its summary -
    up to `OLLAMA_TIMEOUT_SECONDS`. The checkpoint commits the read transaction
    and hands the connection back before the agent is called.
    """
    svc, uow, amara_id = person_with_history()

    await svc.summarize_person(amara_id)

    assert uow.checkpoints == 1


async def test_a_person_with_no_messages_still_releases_the_connection() -> None:
    """The early return must not skip the checkpoint - it is taken *after* it."""
    amara = make_user()
    uow = FakeUnitOfWork(users=[amara], messages=[])

    await build(uow).summarize_person(amara.id)

    assert uow.checkpoints == 1
