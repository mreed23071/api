"""The department hierarchy, against a real database.

Two of these assertions are about constraints rather than code, which is the
point: they are the guarantees the rest of the system is allowed to assume.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.domains.organization.models import OrgNode, OrgNodeMember
from tests.factories import make_user

pytestmark = pytest.mark.integration


async def _node(session, name: str, parent: OrgNode | None = None) -> OrgNode:
    node = OrgNode(name=name, parent_id=parent.id if parent else None)
    session.add(node)
    await session.flush()
    return node


async def test_a_node_round_trips_with_its_parent(session) -> None:
    root = await _node(session, "Acme")
    child = await _node(session, "Engineering", root)

    fetched = await session.get(OrgNode, child.id)
    assert fetched is not None
    assert fetched.parent_id == root.id


async def test_a_person_belongs_to_exactly_one_department(session) -> None:
    """The invariant authorization depends on, enforced by the database.

    Collecting someone's inherited grants is a single walk to the root only
    because this cannot be violated.
    """
    engineering = await _node(session, "Engineering")
    finance = await _node(session, "Finance")
    user = make_user()
    session.add(user)
    await session.flush()

    session.add(OrgNodeMember(org_node_id=engineering.id, user_id=user.id))
    await session.flush()

    session.add(OrgNodeMember(org_node_id=finance.id, user_id=user.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_node_orphans_its_children_rather_than_removing_them(
    session,
) -> None:
    """SET NULL, not CASCADE. Losing a subtree to one delete is unrecoverable."""
    root = await _node(session, "Acme")
    child = await _node(session, "Engineering", root)

    await session.delete(root)
    await session.flush()
    session.expire_all()

    survivor = await session.get(OrgNode, child.id)
    assert survivor is not None, "the child must survive its parent"
    assert survivor.parent_id is None


async def test_deleting_a_node_removes_its_memberships(session) -> None:
    node = await _node(session, "Engineering")
    user = make_user()
    session.add(user)
    await session.flush()
    session.add(OrgNodeMember(org_node_id=node.id, user_id=user.id))
    await session.flush()

    await session.delete(node)
    await session.flush()

    remaining = await session.execute(
        select(OrgNodeMember).where(OrgNodeMember.user_id == user.id)
    )
    assert remaining.scalars().all() == []
