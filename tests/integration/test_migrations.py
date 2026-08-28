"""The migration is what ships; it gets tested like production code."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {"users", "user_relations", "messages", "alembic_version"}


async def test_every_table_exists(session) -> None:
    tables = await session.run_sync(lambda sync: set(inspect(sync.bind).get_table_names()))
    assert EXPECTED_TABLES <= tables


async def test_the_vector_extension_is_installed(session) -> None:
    result = await session.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    )
    assert result.scalar_one_or_none() == 1


async def test_the_platform_enum_carries_every_member(session) -> None:
    """A Platform added in Python without a migration breaks inserts at runtime."""
    from app.domains.identity.models import Platform

    result = await session.execute(
        text(
            "SELECT enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'platform'"
        )
    )
    assert {row[0] for row in result} == {member.value for member in Platform}


async def test_the_idempotency_constraint_exists(session) -> None:
    """The ingestion endpoint's safety depends on this exact constraint name."""
    from app.domains.messaging.repository import IDEMPOTENCY_CONSTRAINT

    result = await session.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": IDEMPOTENCY_CONSTRAINT},
    )
    assert result.scalar_one_or_none() == 1


async def test_the_vector_index_exists_and_is_hnsw(session) -> None:
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_messages_embedding_hnsw'")
    )
    definition = result.scalar_one()
    assert "USING hnsw" in definition
    assert "vector_cosine_ops" in definition


async def test_the_embedding_column_matches_the_configured_dimension(session) -> None:
    from app.core.config import get_settings

    result = await session.execute(
        text(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "WHERE a.attrelid = 'messages'::regclass AND a.attname = 'embedding'"
        )
    )
    assert result.scalar_one() == f"vector({get_settings().embedding_dim})"


async def test_deleting_a_user_cascades_to_their_data(session) -> None:
    """Erasure requests depend on this; verify it rather than assume it."""
    from sqlalchemy import select

    from app.domains.identity.models import User, UserRelation
    from app.domains.messaging.models import Message
    from tests.factories import make_message, make_relation, make_user

    user = make_user()
    session.add(user)
    await session.flush()
    relation = make_relation(user)
    session.add(relation)
    await session.flush()
    session.add(make_message(user, sender_relation_id=relation.id))
    await session.flush()

    await session.delete(user)
    await session.flush()

    assert (await session.execute(select(User))).scalars().all() == []
    assert (await session.execute(select(UserRelation))).scalars().all() == []
    assert (await session.execute(select(Message))).scalars().all() == []
