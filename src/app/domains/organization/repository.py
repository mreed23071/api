"""Data access for the organization context.

A "repository" here is the only place that talks to the database about
departments. Everything above it - the service, the routes - works with Python
objects and never writes a query, which is what makes the storage swappable and
the business logic testable without a database running.

Two conventions to know when reading this file:

* `async def` / `await`. Every method that touches the database is a coroutine:
  calling it returns immediately with a promise, and `await` is where the caller
  actually waits. While one request waits on the database the process serves
  others, which is how one Python process handles many concurrent requests.
* `self.scoped(...)`. Every SELECT is wrapped in it. Today it returns the query
  unchanged; later it will add "and only rows this caller may see". Routing
  every read through one method is what makes that a one-line change instead of
  an audit of every query. `tests/contract/test_layering.py` fails the build if
  a SELECT here ever skips it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select

from app.core.db.repository import Repository
from app.domains.organization.models import OrgNode, OrgNodeMember


class OrgNodeRepository(Repository):
    """Reads and writes rows in the `org_nodes` table."""

    async def list_all(self) -> Sequence[OrgNode]:
        """Fetch every department, oldest first.

        Emits one `SELECT * FROM org_nodes ORDER BY created_at, id`. The tree is
        returned flat - each row carries its own `parent_id` - and the caller
        reassembles the shape. That is deliberate: an org chart is bounded by
        headcount, every caller wants the whole thing anyway, and one query is
        cheaper than walking the hierarchy level by level.

        The secondary sort on `id` only matters for rows created in the same
        instant: without it their order could differ between calls, which makes
        tests flaky for no reason.
        """
        statement = self.scoped(select(OrgNode), OrgNode).order_by(
            OrgNode.created_at.asc(), OrgNode.id.asc()
        )
        return (await self.session.execute(statement)).scalars().all()

    async def get(self, node_id: uuid.UUID) -> OrgNode | None:
        """Fetch one department by id, or `None` if there is no such row.

        `-> OrgNode | None` is Python's way of saying "an OrgNode or nothing";
        the caller has to handle both. `scalar_one_or_none()` says the same at
        the query level: return the single row, or `None` - and raise if the
        query somehow matched more than one, which for a primary-key lookup
        would mean something is badly wrong.
        """
        statement = self.scoped(select(OrgNode), OrgNode).where(OrgNode.id == node_id)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def add(self, node: OrgNode) -> OrgNode:
        """Stage a new department for insertion and fill in its generated id.

        Two steps, and the difference matters. `session.add` only queues the row
        in memory. `flush` sends the INSERT to the database - which is what makes
        the server-generated `id` and `created_at` readable - but does *not*
        commit. The transaction is still open and can still be rolled back; the
        service decides when to commit.
        """
        self.session.add(node)
        await self.session.flush()
        return node

    async def children_of(self, node_id: uuid.UUID) -> Sequence[OrgNode]:
        """Fetch the departments directly beneath one department.

        Direct children only, not the whole subtree. Used when deleting a
        department, to move its children up to their grandparent.
        """
        statement = self.scoped(select(OrgNode), OrgNode).where(OrgNode.parent_id == node_id)
        return (await self.session.execute(statement)).scalars().all()

    async def remove(self, node: OrgNode) -> None:
        """Delete one department.

        Its memberships go too, via the `ON DELETE CASCADE` on the membership
        table. Its child departments do not - that foreign key is
        `ON DELETE SET NULL` - which is why the service explicitly re-points
        them at the grandparent before calling this.
        """
        await self.session.delete(node)
        await self.session.flush()


class OrgNodeMemberRepository(Repository):
    """Reads and writes rows in the `org_node_members` table.

    One row per person who has been filed into a department. A person with no
    row here belongs to no department, which is a legitimate state.
    """

    async def list_all(self) -> Sequence[OrgNodeMember]:
        """Fetch every membership in one query.

        Used by the department list, which needs the members of every node. The
        alternative - asking once per department - is the pattern that looks
        fine with five departments and issues two hundred queries with two
        hundred.
        """
        statement = self.scoped(select(OrgNodeMember), OrgNodeMember)
        return (await self.session.execute(statement)).scalars().all()

    async def for_node(self, node_id: uuid.UUID) -> Sequence[OrgNodeMember]:
        """Fetch the memberships of one department."""
        statement = self.scoped(select(OrgNodeMember), OrgNodeMember).where(
            OrgNodeMember.org_node_id == node_id
        )
        return (await self.session.execute(statement)).scalars().all()

    async def for_users(self, user_ids: Sequence[uuid.UUID]) -> Sequence[OrgNodeMember]:
        """Fetch the memberships of any of these people.

        What a paginated directory listing uses instead of `list_all` - its
        query cost tracks the page size rather than every membership that exists.
        """
        if not user_ids:
            return []
        statement = self.scoped(select(OrgNodeMember), OrgNodeMember).where(
            OrgNodeMember.user_id.in_(user_ids)
        )
        return (await self.session.execute(statement)).scalars().all()

    async def for_user(self, user_id: uuid.UUID) -> OrgNodeMember | None:
        """Fetch the one membership a person has, or `None` if unfiled.

        `scalar_one_or_none` rather than `first`: the unique constraint on
        `user_id` guarantees at most one row, so if two ever came back this
        would raise rather than quietly return one of them. That is the desired
        behaviour - it is how we would find out the constraint had been dropped.
        """
        statement = self.scoped(select(OrgNodeMember), OrgNodeMember).where(
            OrgNodeMember.user_id == user_id
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def add(self, membership: OrgNodeMember) -> OrgNodeMember:
        """File a person into a department.

        Fails if they are already in one - the database's unique constraint on
        `user_id` rejects the second row. Callers move people by removing first.
        """
        self.session.add(membership)
        await self.session.flush()
        return membership

    async def remove_for_user(self, user_id: uuid.UUID) -> int:
        """Unfile a person, and report how many rows that removed - 0 or 1.

        A bulk `DELETE ... WHERE user_id = ...` rather than load-then-delete: it
        is one round trip instead of two, and it does not care whether the row
        exists. `rowcount` is how many rows the statement affected; the
        `or 0` guards against drivers that report `None` when they cannot tell.
        """
        result = await self.session.execute(
            delete(OrgNodeMember).where(OrgNodeMember.user_id == user_id)
        )
        await self.session.flush()
        # DELETE executes to a CursorResult, but `execute`'s declared return
        # type is the narrower `Result[Any]`, which has no `rowcount` - the
        # cast reflects what's actually running, not a guess.
        return int(cast(CursorResult[Any], result).rowcount or 0)
