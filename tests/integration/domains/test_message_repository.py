"""Message persistence, the vector column, and the queries a fake cannot verify."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.domains.identity.models import Platform
from app.domains.messaging.dto import NewMessage
from app.domains.messaging.models import Message
from tests.factories import make_relation, make_user

pytestmark = pytest.mark.integration

#: Derived, never hardcoded: the column's width follows EMBEDDING_DIM, and a
#: literal here would fail the day the embedding model changes rather than the
#: day something is actually wrong.
DIM = get_settings().embedding_dim


def new_message(user, relation, **overrides) -> NewMessage:  # type: ignore[no-untyped-def]
    defaults = {
        "platform": Platform.SLACK,
        "external_message_id": "slack-0001",
        "conversation_id": "slack-general",
        "content": "The deploy is green.",
        "sent_at": datetime.now(UTC),
        "source_metadata": {"source": "test"},
        "sender_user_id": user.id,
        "sender_relation_id": relation.id,
        "embedding": [0.1] * DIM,
        "embedding_model": "test-embedder",
        "filter_category": "business",
        "filter_reason": "matched business markers",
        "filter_prompt_version": "v1",
    }
    return NewMessage(**{**defaults, **overrides})


@pytest.fixture
async def sender(uow):  # type: ignore[no-untyped-def]
    user = await uow.users.add(make_user())
    relation = await uow.relations.add(make_relation(user))
    return user, relation


async def test_a_message_round_trips_with_its_vector(uow, sender) -> None:
    """Verifies the asyncpg codec for the `vector` type end to end."""
    user, relation = sender
    await uow.messages.bulk_upsert([new_message(user, relation)])

    stored = (await uow.session.execute(select(Message))).scalar_one()
    assert len(stored.embedding) == DIM
    assert pytest.approx(float(stored.embedding[0]), abs=1e-6) == 0.1
    assert stored.filter_prompt_version == "v1"


async def test_reingesting_the_same_message_writes_nothing(uow, sender) -> None:
    """The guarantee that makes the cron endpoint safe to retry."""
    user, relation = sender
    first = await uow.messages.bulk_upsert([new_message(user, relation)])
    second = await uow.messages.bulk_upsert([new_message(user, relation)])

    assert len(first) == 1
    assert second == []
    assert await uow.messages.count_for_user(user.id) == 1


async def test_the_same_external_id_on_another_platform_is_a_different_message(uow, sender) -> None:
    user, relation = sender
    await uow.messages.bulk_upsert([new_message(user, relation, platform=Platform.SLACK)])
    written = await uow.messages.bulk_upsert(
        [new_message(user, relation, platform=Platform.GITHUB)]
    )
    assert len(written) == 1


async def test_existing_keys_matches_on_the_full_composite_key(uow, sender) -> None:
    user, relation = sender
    await uow.messages.bulk_upsert(
        [new_message(user, relation, platform=Platform.SLACK, external_message_id="shared")]
    )

    known = await uow.messages.existing_keys(
        [(Platform.SLACK, "shared"), (Platform.GITHUB, "shared")]
    )
    assert known == {(Platform.SLACK, "shared")}


async def test_latest_for_users_returns_newest_first_per_user(uow) -> None:
    now = datetime.now(UTC)
    users = []
    for index in range(3):
        user = await uow.users.add(make_user())
        relation = await uow.relations.add(make_relation(user))
        users.append(user)
        await uow.messages.bulk_upsert(
            [
                new_message(
                    user,
                    relation,
                    external_message_id=f"m-{index}-{age}",
                    content=f"message {age}",
                    sent_at=now - timedelta(hours=age),
                )
                for age in range(5)
            ]
        )

    grouped = await uow.messages.latest_for_users([u.id for u in users], per_user_limit=3)

    assert set(grouped) == {u.id for u in users}
    for messages in grouped.values():
        assert len(messages) == 3
        assert [m.content for m in messages] == ["message 0", "message 1", "message 2"]


async def test_latest_for_users_does_not_leak_between_users(uow) -> None:
    first = await uow.users.add(make_user())
    second = await uow.users.add(make_user())
    relation = await uow.relations.add(make_relation(first))
    await uow.messages.bulk_upsert([new_message(first, relation)])

    grouped = await uow.messages.latest_for_users([first.id, second.id])
    assert len(grouped[first.id]) == 1
    assert grouped[second.id] == []


async def test_latest_for_users_with_no_ids_makes_no_query(uow) -> None:
    assert await uow.messages.latest_for_users([]) == {}


async def test_bulk_upsert_of_nothing_is_a_no_op(uow) -> None:
    assert await uow.messages.bulk_upsert([]) == []


async def test_the_hnsw_index_answers_a_nearest_neighbour_query(uow, sender) -> None:
    """The index is expensive to maintain; prove it is usable.

    No application code queries it yet (S-1 in the prototype report). This test
    is the evidence that the column and index are correct when a search endpoint
    is finally written.
    """
    user, relation = sender
    near = [0.1] * DIM
    far = [-0.9] + [0.0] * (DIM - 1)
    await uow.messages.bulk_upsert(
        [
            new_message(user, relation, external_message_id="near", embedding=near),
            new_message(user, relation, external_message_id="far", embedding=far),
        ]
    )

    result = await uow.session.execute(
        text(
            "SELECT external_message_id FROM messages "
            "ORDER BY embedding <=> CAST(:probe AS vector) LIMIT 1"
        ),
        {"probe": str(near)},
    )
    assert result.scalar_one() == "near"
