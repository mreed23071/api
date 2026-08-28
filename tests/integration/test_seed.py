"""Seeding, against a real database.

The unit tests check the fixture file is coherent. This checks it actually
lands - that every foreign key, constraint and enum in the schema accepts it,
which is the only way to know the demo stack will come up.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.domains.identity.models import PersonNote, User, UserRelation
from app.domains.ingestion.models import IngestionRun
from app.domains.messaging.models import Message
from app.domains.organization.models import OrgNode, OrgNodeMember
from app.seed.loader import load_fixtures, seed_database

pytestmark = pytest.mark.integration


async def count(session, model) -> int:  # type: ignore[no-untyped-def]
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_the_whole_dataset_lands(session) -> None:  # type: ignore[no-untyped-def]
    fixtures = load_fixtures()

    report = await seed_database(session)

    assert report.users == len(fixtures["people"])
    assert report.relations == len(fixtures["connected_accounts"])
    assert report.messages == len(fixtures["messages"])
    assert await count(session, User) == len(fixtures["people"])
    assert await count(session, Message) == len(fixtures["messages"])
    assert await count(session, PersonNote) == len(fixtures["person_notes"])
    assert await count(session, OrgNode) == len(fixtures["org_nodes"])
    assert await count(session, IngestionRun) == len(fixtures["ingestion_runs"])


async def test_seeding_twice_inserts_nothing_the_second_time(session) -> None:  # type: ignore[no-untyped-def]
    """Idempotency is what lets the container seed itself on every restart."""
    first = await seed_database(session)
    second = await seed_database(session)

    assert first.total > 0
    assert second.total == 0
    assert await count(session, User) == first.users


async def test_a_half_seeded_database_is_completed_rather_than_duplicated(session) -> None:  # type: ignore[no-untyped-def]
    fixtures = load_fixtures()
    people_only = {"people": fixtures["people"]}

    await seed_database(session, fixtures=people_only)
    report = await seed_database(session)

    assert report.users == 0, "the people were already there"
    assert report.messages == len(fixtures["messages"])
    assert await count(session, User) == len(fixtures["people"])


async def test_unlinked_accounts_and_orphaned_messages_survive_the_load(session) -> None:  # type: ignore[no-untyped-def]
    """The nullable columns the console depends on, proven against real constraints."""
    await seed_database(session)

    unlinked = await session.execute(
        select(func.count()).select_from(UserRelation).where(UserRelation.user_id.is_(None))
    )
    orphaned = await session.execute(
        select(func.count()).select_from(Message).where(Message.sender_user_id.is_(None))
    )

    assert int(unlinked.scalar_one()) > 0
    assert int(orphaned.scalar_one()) > 0


async def test_commit_detail_round_trips_through_the_metadata_blob(session) -> None:  # type: ignore[no-untyped-def]
    await seed_database(session)

    row = await session.execute(select(Message).where(Message.kind == "commit").limit(1))
    commit = row.scalars().one()

    assert commit.source_metadata["commit"]["sha"]
    assert commit.source_metadata["commit"]["files"]


async def test_every_membership_points_at_a_department_and_a_person(session) -> None:  # type: ignore[no-untyped-def]
    await seed_database(session)

    memberships = (await session.execute(select(OrgNodeMember))).scalars().all()
    user_ids = set((await session.execute(select(User.id))).scalars().all())
    node_ids = set((await session.execute(select(OrgNode.id))).scalars().all())

    assert memberships
    assert all(m.user_id in user_ids and m.org_node_id in node_ids for m in memberships)
