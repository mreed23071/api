"""Organization service behaviour, against the in-memory unit of work."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import NotFoundError, ValidationError
from app.core.security.principal import Principal
from app.domains.organization.dto import NewOrgNode, OrgNodePatch
from app.domains.organization.models import OrgNode, OrgNodeMember
from app.domains.organization.service import OrganizationService
from tests.factories import make_user
from tests.fakes.uow import FakeUnitOfWork


def node(name: str, parent_id: uuid.UUID | None = None) -> OrgNode:
    entity = OrgNode(name=name, parent_id=parent_id)
    entity.id = uuid.uuid4()
    entity.created_at = datetime.now(UTC)
    return entity


def service(**kwargs) -> tuple[OrganizationService, FakeUnitOfWork]:  # type: ignore[no-untyped-def]
    uow = FakeUnitOfWork(**kwargs)
    return OrganizationService(uow, Principal.anonymous()), uow


async def test_listing_folds_membership_into_each_node() -> None:
    engineering, finance = node("Engineering"), node("Finance")
    amara, daniel = make_user(), make_user()
    svc, _ = service(
        users=[amara, daniel],
        org_nodes=[engineering, finance],
        org_members=[
            OrgNodeMember(id=uuid.uuid4(), org_node_id=engineering.id, user_id=amara.id),
            OrgNodeMember(id=uuid.uuid4(), org_node_id=finance.id, user_id=daniel.id),
        ],
    )

    views = {view.name: view for view in await svc.list_nodes()}

    assert views["Engineering"].member_ids == [amara.id]
    assert views["Finance"].member_ids == [daniel.id]


async def test_creating_a_node_under_an_unknown_parent_is_refused() -> None:
    svc, _ = service()

    with pytest.raises(NotFoundError):
        await svc.create_node(NewOrgNode(name="Platform", parent_id=uuid.uuid4()))


async def test_a_department_needs_a_name() -> None:
    svc, _ = service()

    with pytest.raises(ValidationError):
        await svc.create_node(NewOrgNode(name="   "))


async def test_a_node_cannot_be_moved_under_its_own_descendant() -> None:
    """The guard that keeps the hierarchy a tree."""
    engineering = node("Engineering")
    platform = node("Platform", engineering.id)
    svc, _ = service(org_nodes=[engineering, platform])

    with pytest.raises(ValidationError, match="sub-departments"):
        await svc.update_node(
            engineering.id, OrgNodePatch(parent_id=platform.id, reparent=True)
        )


async def test_a_node_can_be_promoted_to_a_root() -> None:
    """`reparent` with `None` means "make this a root", not "leave it alone"."""
    engineering = node("Engineering")
    platform = node("Platform", engineering.id)
    svc, _ = service(org_nodes=[engineering, platform])

    updated = await svc.update_node(platform.id, OrgNodePatch(parent_id=None, reparent=True))

    assert updated.parent_id is None


async def test_a_patch_without_reparent_leaves_the_parent_alone() -> None:
    engineering = node("Engineering")
    platform = node("Platform", engineering.id)
    svc, _ = service(org_nodes=[engineering, platform])

    updated = await svc.update_node(platform.id, OrgNodePatch(name="Platform Team"))

    assert updated.name == "Platform Team"
    assert updated.parent_id == engineering.id


async def test_deleting_a_node_promotes_its_children_to_the_grandparent() -> None:
    """SET NULL would scatter them to roots; promotion keeps the shape."""
    acme = node("Acme")
    engineering = node("Engineering", acme.id)
    platform = node("Platform", engineering.id)
    delivery = node("Delivery", engineering.id)
    svc, _ = service(org_nodes=[acme, engineering, platform, delivery])

    result = await svc.delete_node(engineering.id)

    assert result.promoted == 2
    assert platform.parent_id == acme.id
    assert delivery.parent_id == acme.id


async def test_assigning_a_person_moves_them_out_of_their_old_department() -> None:
    """One department per person - so this is a move, never an add."""
    engineering, finance = node("Engineering"), node("Finance")
    amara = make_user()
    svc, uow = service(
        users=[amara],
        org_nodes=[engineering, finance],
        org_members=[
            OrgNodeMember(id=uuid.uuid4(), org_node_id=engineering.id, user_id=amara.id)
        ],
    )

    view = await svc.assign_member(finance.id, amara.id)

    assert view.member_ids == [amara.id]
    assert len(uow.org_members.memberships) == 1
    assert await svc.department_of(amara.id) == finance.id


async def test_assigning_an_unknown_person_is_refused() -> None:
    engineering = node("Engineering")
    svc, _ = service(org_nodes=[engineering])

    with pytest.raises(NotFoundError):
        await svc.assign_member(engineering.id, uuid.uuid4())


async def test_removing_someone_from_a_department_they_are_not_in_is_refused() -> None:
    engineering, finance = node("Engineering"), node("Finance")
    amara = make_user()
    svc, _ = service(
        users=[amara],
        org_nodes=[engineering, finance],
        org_members=[
            OrgNodeMember(id=uuid.uuid4(), org_node_id=engineering.id, user_id=amara.id)
        ],
    )

    with pytest.raises(NotFoundError):
        await svc.remove_member(finance.id, amara.id)


async def test_an_unfiled_person_has_no_department() -> None:
    svc, _ = service(users=[make_user()])

    assert await svc.department_of(uuid.uuid4()) is None
