"""Summarising one person, optionally over a slice of their history."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.security.principal import Principal
from app.domains.insights.dto import SummaryWindow
from app.domains.insights.service import UserInsightsService
from app.shared.llm.stub import StubLLMClient
from tests.factories import make_message, make_user
from tests.fakes.uow import FakeUnitOfWork

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def build(uow: FakeUnitOfWork) -> UserInsightsService:
    return UserInsightsService(
        uow=uow,
        principal=Principal.anonymous(),
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
