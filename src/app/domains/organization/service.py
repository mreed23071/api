"""Organization business logic - the department hierarchy and who is in it."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from app.core.errors import NotFoundError, ValidationError
from app.core.security.principal import Principal
from app.core.security.provisional import require_console_access
from app.domains.organization.dto import (
    DeletionResult,
    NewOrgNode,
    OrgNodePatch,
    OrgNodeView,
)
from app.domains.organization.models import OrgNode, OrgNodeMember
from app.domains.organization.tree import would_create_cycle
from app.domains.uow import UnitOfWork

logger = logging.getLogger(__name__)


class OrganizationService:
    """Reads and edits the department tree.

    Takes the `Principal` rather than trusting the route to have checked, for
    the same reason every other service here does: the access decision lives
    next to the behaviour it protects, so it survives the route being reused,
    wrapped in a worker, or called from a test.
    """

    def __init__(self, uow: UnitOfWork, principal: Principal) -> None:
        self.uow = uow
        self.principal = principal

    # -- reads --------------------------------------------------------------

    async def list_nodes(self) -> list[OrgNodeView]:
        """Return every department, flat, each with the ids of its members.

        Two queries regardless of depth or size: the nodes, and every
        membership. Grouping in Python beats a per-node query, and the data is
        bounded by headcount rather than by message volume.
        """
        require_console_access(self.principal)
        nodes = await self.uow.org_nodes.list_all()
        memberships = await self.uow.org_members.list_all()

        by_node: dict[uuid.UUID, list[uuid.UUID]] = {}
        for membership in memberships:
            by_node.setdefault(membership.org_node_id, []).append(membership.user_id)

        return [self._view(node, by_node.get(node.id, [])) for node in nodes]

    async def department_of(self, user_id: uuid.UUID) -> uuid.UUID | None:
        """Which department a person is in, if any.

        The console shows this on the employee list, and authorization will
        later start every check with it.
        """
        require_console_access(self.principal)
        membership = await self.uow.org_members.for_user(user_id)
        return membership.org_node_id if membership else None

    # -- writes -------------------------------------------------------------

    async def create_node(self, new: NewOrgNode) -> OrgNodeView:
        """Create a department, optionally beneath an existing one.

        `new.name.strip()` removes surrounding whitespace - a form that submits
        three spaces should be rejected as empty, not accepted as a department
        called "   ".

        The parent is checked *before* the transaction opens. Validating first
        and writing second means a bad request never starts a transaction it
        cannot finish.
        """
        require_console_access(self.principal)
        name = new.name.strip()
        if not name:
            raise ValidationError("A department needs a name.")

        if new.parent_id is not None:
            await self._require_node(new.parent_id)

        async with self.uow.transaction():
            node = await self.uow.org_nodes.add(
                OrgNode(name=name, subtitle=new.subtitle, parent_id=new.parent_id)
            )
        return self._view(node, [])

    async def update_node(self, node_id: uuid.UUID, patch: OrgNodePatch) -> OrgNodeView:
        """Rename a department, retitle it, or move it somewhere else in the tree.

        A patch only changes the fields it names. `None` means "leave alone" for
        the text fields - but not for the parent, where `None` is a real value
        meaning "make this a root". That is why the patch carries a separate
        `reparent` flag: without it, "promote to root" and "do not touch the
        parent" would look identical.

        `async with self.uow.transaction():` opens a database transaction and
        closes it at the end of the indented block - committing if the block
        finished, rolling back if it raised. Nothing is written unless the whole
        block succeeds.
        """
        require_console_access(self.principal)
        node = await self._require_node(node_id)

        if patch.reparent:
            await self._check_reparent(node_id, patch.parent_id)

        async with self.uow.transaction():
            if patch.name is not None:
                name = patch.name.strip()
                if not name:
                    raise ValidationError("A department needs a name.")
                node.name = name
            if patch.subtitle is not None:
                node.subtitle = patch.subtitle
            if patch.reparent:
                node.parent_id = patch.parent_id
            await self.uow.flush()

        members = await self.uow.org_members.for_node(node_id)
        return self._view(node, [m.user_id for m in members])

    async def delete_node(self, node_id: uuid.UUID) -> DeletionResult:
        """Delete a department and promote its children to its parent.

        Promotion is done here rather than left to the foreign key, which is
        `ON DELETE SET NULL` and would scatter the children to the roots. A
        department disappearing should move its sub-departments up one level,
        not detach them from the organization.
        """
        require_console_access(self.principal)
        node = await self._require_node(node_id)
        children = await self.uow.org_nodes.children_of(node_id)

        async with self.uow.transaction():
            for child in children:
                child.parent_id = node.parent_id
            await self.uow.flush()
            await self.uow.org_nodes.remove(node)

        return DeletionResult(id=node_id, promoted=len(children))

    async def assign_member(self, node_id: uuid.UUID, user_id: uuid.UUID) -> OrgNodeView:
        """File a person into a department, moving them out of any other.

        A person belongs to exactly one department, so this is a move rather
        than an add. The old membership is removed first: the database would
        refuse the second row anyway, and failing on a unique constraint is a
        worse way to express "they were somewhere else".
        """
        require_console_access(self.principal)
        node = await self._require_node(node_id)
        await self._require_user(user_id)

        async with self.uow.transaction():
            await self.uow.org_members.remove_for_user(user_id)
            await self.uow.org_members.add(OrgNodeMember(org_node_id=node_id, user_id=user_id))

        members = await self.uow.org_members.for_node(node_id)
        return self._view(node, [m.user_id for m in members])

    async def remove_member(self, node_id: uuid.UUID, user_id: uuid.UUID) -> OrgNodeView:
        """Unfile a person from a department, leaving them unassigned.

        Refuses if they are in a *different* department. Silently succeeding
        there would let a stale console screen quietly unfile somebody the user
        was not looking at.
        """
        require_console_access(self.principal)
        node = await self._require_node(node_id)
        membership = await self.uow.org_members.for_user(user_id)

        if membership is None or membership.org_node_id != node_id:
            raise NotFoundError(
                "That person is not in that department.",
                details={"org_node_id": str(node_id), "user_id": str(user_id)},
            )

        async with self.uow.transaction():
            await self.uow.org_members.remove_for_user(user_id)

        members = await self.uow.org_members.for_node(node_id)
        return self._view(node, [m.user_id for m in members])

    # -- internals ----------------------------------------------------------

    async def _require_node(self, node_id: uuid.UUID) -> OrgNode:
        """Fetch a department, or raise `NotFoundError` if it does not exist.

        The leading underscore is a convention, not enforcement: it marks this
        as internal to the class, so nothing outside should call it.

        Raising rather than returning `None` is what lets every caller above
        assume the department exists. The error carries `details`, which the API
        layer turns into a 404 with a machine-readable body.
        """
        node = await self.uow.org_nodes.get(node_id)
        if node is None:
            raise NotFoundError("Department not found.", details={"org_node_id": str(node_id)})
        return node

    async def _require_user(self, user_id: uuid.UUID) -> None:
        """Assert a person exists, raising `NotFoundError` if not.

        Returns nothing - it is called purely for the exception. Filing an
        unknown user id into a department would otherwise create a membership
        row pointing at nobody, which the foreign key would reject anyway but
        with a far less useful error.
        """
        if await self.uow.users.get(user_id) is None:
            raise NotFoundError("User not found.", details={"user_id": str(user_id)})

    async def _check_reparent(self, node_id: uuid.UUID, next_parent_id: uuid.UUID | None) -> None:
        """Refuse a move that would detach a subtree from the tree.

        The whole tree is loaded for this, which is the honest cost of the
        check: whether a move is legal depends on every node beneath the one
        being moved, and there is no cheaper question to ask.
        """
        if next_parent_id is None:
            return
        await self._require_node(next_parent_id)

        nodes: Sequence[OrgNode] = await self.uow.org_nodes.list_all()
        if would_create_cycle(nodes, node_id, next_parent_id):
            raise ValidationError(
                "A department cannot report to itself or to one of its own sub-departments.",
                details={"org_node_id": str(node_id), "parent_id": str(next_parent_id)},
            )

    @staticmethod
    def _view(node: OrgNode, member_ids: Sequence[uuid.UUID]) -> OrgNodeView:
        """Convert a database row into the shape the rest of the app passes around.

        A `@staticmethod` is a function that lives on the class for tidiness but
        needs nothing from `self` - no database, no principal, just its inputs.

        The conversion exists so that nothing outside this context handles a
        live ORM object. Those are attached to a session and can trigger
        surprise queries when read; a plain view object cannot.
        """
        return OrgNodeView(
            id=node.id,
            name=node.name,
            subtitle=node.subtitle,
            parent_id=node.parent_id,
            created_at=node.created_at,
            member_ids=list(member_ids),
        )
