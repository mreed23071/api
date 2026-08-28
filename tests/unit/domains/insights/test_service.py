"""Retrieval + summarization behaviour, with every port faked."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.pagination import PageParams
from app.core.security.principal import Principal, Scope
from app.domains.insights.service import NO_MESSAGES_SUMMARY, UserInsightsService
from app.shared.llm.stub import StubLLMClient
from tests.conftest import make_principal
from tests.factories import make_message, make_relation, make_user
from tests.fakes import FailingLLMClient, FakeUnitOfWork


def build(uow, llm=None, principal=None):  # type: ignore[no-untyped-def]
    return UserInsightsService(
        uow,
        principal or make_principal(Scope.INSIGHTS_READ, Scope.MESSAGES_READ),
        llm or StubLLMClient(),
        get_settings(),
    )


def seeded_uow(user_count: int = 2, messages_each: int = 3) -> FakeUnitOfWork:
    users, relations, messages = [], [], []
    for index in range(user_count):
        user = make_user(full_name=f"Person {index}")
        relation = make_relation(user)
        user.__dict__["relations"] = [relation]
        users.append(user)
        relations.append(relation)
        messages.extend(make_message(user) for _ in range(messages_each))
    return FakeUnitOfWork(users=users, relations=relations, messages=messages)


async def test_every_user_on_the_page_gets_a_summary() -> None:
    result = await build(seeded_uow()).list_with_summaries(PageParams())

    assert result.page.total == 2
    assert len(result.page.items) == 2
    assert all(entry.summary for entry in result.page.items)
    assert all(entry.summary_error is None for entry in result.page.items)


async def test_linked_identities_travel_with_the_user() -> None:
    result = await build(seeded_uow()).list_with_summaries(PageParams())
    assert all(len(entry.relations) == 1 for entry in result.page.items)


async def test_recent_messages_are_capped_at_five() -> None:
    result = await build(seeded_uow(user_count=1, messages_each=9)).list_with_summaries(
        PageParams()
    )
    entry = result.page.items[0]
    assert entry.message_count == 9
    assert len(entry.recent_messages) == 5


async def test_a_user_with_no_messages_costs_no_model_call() -> None:
    uow = FakeUnitOfWork(users=[make_user()])
    llm = FailingLLMClient()  # would raise if it were called
    result = await build(uow, llm=llm).list_with_summaries(PageParams())

    assert result.page.items[0].summary == NO_MESSAGES_SUMMARY
    assert llm.call_count == 0


async def test_one_failing_user_does_not_fail_the_page() -> None:
    result = await build(seeded_uow(), llm=FailingLLMClient()).list_with_summaries(PageParams())

    assert len(result.page.items) == 2
    assert all(entry.summary is None for entry in result.page.items)
    assert all("provider is down" in (entry.summary_error or "") for entry in result.page.items)


async def test_pagination_is_reflected_in_the_result() -> None:
    result = await build(seeded_uow(user_count=5)).list_with_summaries(
        PageParams(limit=2, offset=2)
    )
    assert len(result.page.items) == 2
    assert result.page.total == 5
    assert result.page.has_more is True


async def test_provenance_names_the_provider_and_model() -> None:
    result = await build(seeded_uow()).list_with_summaries(PageParams())
    assert result.llm_provider == "stub"
    assert result.llm_model


async def test_reading_summaries_requires_both_scopes() -> None:
    uow = seeded_uow()
    with pytest.raises(AuthorizationError):
        await build(uow, principal=make_principal(Scope.INSIGHTS_READ)).list_with_summaries(
            PageParams()
        )


async def test_anonymous_callers_cannot_read_summaries() -> None:
    with pytest.raises(AuthenticationError):
        await build(seeded_uow(), principal=Principal.anonymous()).list_with_summaries(PageParams())
