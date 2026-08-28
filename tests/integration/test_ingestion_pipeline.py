"""The whole pipeline against a real database.

The unit suite proves the branching; this proves it survives contact with
PostgreSQL - real constraints, real enum, real vector column, real transaction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security.principal import Scope
from app.domains.identity.models import Platform, User, UserRelation
from app.domains.ingestion.dto import IngestionOptions
from app.domains.ingestion.service import IngestionService
from app.domains.ingestion.sources import MockChatSource
from app.domains.messaging.models import Message
from app.shared.llm.stub import StubLLMClient
from tests.conftest import make_principal
from tests.fakes import FakeEmbeddingService

pytestmark = pytest.mark.integration


@pytest.fixture
def service(uow):  # type: ignore[no-untyped-def]
    embeddings = FakeEmbeddingService()
    embeddings.start()
    return IngestionService(
        uow=uow,
        principal=make_principal(Scope.INGEST_RUN, Scope.INGEST_READ),
        source=MockChatSource(Platform.SLACK, name="slack-mock"),
        llm=StubLLMClient(),
        embeddings=embeddings,
        settings=get_settings(),
    )


async def count(session, model) -> int:  # type: ignore[no-untyped-def]
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_a_full_run_persists_users_relations_and_messages(service, uow) -> None:
    result = await service.run(IngestionOptions(), platform=Platform.SLACK)

    assert result.persisted > 0
    assert await count(uow.session, User) == result.users_provisioned == 3
    assert await count(uow.session, UserRelation) == result.relations_provisioned
    assert await count(uow.session, Message) == result.persisted


async def test_stored_messages_carry_a_real_vector(service, uow) -> None:
    await service.run(IngestionOptions(), platform=Platform.SLACK)
    message = (await uow.session.execute(select(Message).limit(1))).scalar_one()
    assert message.embedding is not None
    assert len(message.embedding) == get_settings().embedding_dim


async def test_a_second_run_is_a_no_op_against_the_real_constraint(service, uow) -> None:
    first = await service.run(IngestionOptions(), platform=Platform.SLACK)
    second = await service.run(IngestionOptions(), platform=Platform.SLACK)

    assert second.persisted == 0
    assert await count(uow.session, Message) == first.persisted


async def test_dry_run_leaves_the_database_untouched(service, uow) -> None:
    result = await service.run(IngestionOptions(dry_run=True), platform=Platform.SLACK)

    assert result.retained > 0
    assert await count(uow.session, Message) == 0
    assert await count(uow.session, User) == 0


async def test_summaries_read_back_what_ingestion_wrote(service, uow) -> None:
    """The two domains meet here; this is the only test that covers the seam."""
    from app.core.pagination import PageParams
    from app.domains.insights.service import UserInsightsService

    await service.run(IngestionOptions(), platform=Platform.SLACK)

    insights = UserInsightsService(
        uow,
        make_principal(Scope.INSIGHTS_READ, Scope.MESSAGES_READ),
        StubLLMClient(),
        get_settings(),
    )
    page = await insights.list_with_summaries(PageParams())

    assert page.page.total == 3
    assert all(entry.summary for entry in page.page.items)
    assert sum(entry.message_count for entry in page.page.items) > 0
