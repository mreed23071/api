"""v1 HTTP surface for the department hierarchy."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.deps import OrganizationServiceDep
from app.api.errors import AUTH_RESPONSES
from app.api.v1.schemas.organization import (
    OrgMemberAssign,
    OrgNodeCreate,
    OrgNodeDeleteResponse,
    OrgNodeRead,
    OrgNodeUpdate,
)
from app.domains.organization.dto import NewOrgNode, OrgNodePatch

router = APIRouter(prefix="/org", tags=["organization"], responses=AUTH_RESPONSES)

NodeId = Annotated[uuid.UUID, Path(description="The department's id.")]


@router.get(
    "/nodes",
    response_model=list[OrgNodeRead],
    summary="Fetch the whole department hierarchy",
    description=(
        "Returns every department flat, each carrying its own `parent_id` and "
        "the ids of the people filed into it. The client rebuilds the tree in "
        "one pass.\n\n"
        "Flat rather than nested on purpose: the response shape stays the same "
        "however deep the hierarchy gets, and moving a department is a "
        "one-field change rather than a restructure."
    ),
)
async def list_org_nodes(service: OrganizationServiceDep) -> list[OrgNodeRead]:
    return [OrgNodeRead.from_view(view) for view in await service.list_nodes()]


@router.post(
    "/nodes",
    response_model=OrgNodeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
)
async def create_org_node(body: OrgNodeCreate, service: OrganizationServiceDep) -> OrgNodeRead:
    view = await service.create_node(
        NewOrgNode(name=body.name, subtitle=body.subtitle, parent_id=body.parent_id)
    )
    return OrgNodeRead.from_view(view)


@router.patch(
    "/nodes/{node_id}",
    response_model=OrgNodeRead,
    summary="Rename a department or move it in the hierarchy",
    description=(
        "Send `reparent: true` to apply `parent_id`, including null to make the "
        "department a root. Without it, `parent_id` would be ambiguous with "
        "'leave the parent alone'.\n\n"
        "Returns 422 if the move would make a department report to itself or to "
        "one of its own sub-departments - which would cut that branch loose "
        "from the organization."
    ),
)
async def update_org_node(
    node_id: NodeId, body: OrgNodeUpdate, service: OrganizationServiceDep
) -> OrgNodeRead:
    view = await service.update_node(
        node_id,
        OrgNodePatch(
            name=body.name,
            subtitle=body.subtitle,
            parent_id=body.parent_id,
            reparent=body.reparent,
        ),
    )
    return OrgNodeRead.from_view(view)


@router.delete(
    "/nodes/{node_id}",
    response_model=OrgNodeDeleteResponse,
    summary="Delete a department, promoting its children",
    description=(
        "Sub-departments move up to the deleted department's parent rather than "
        "being deleted or detached. The response says how many moved, which is "
        "worth showing in a confirmation: deleting a leaf and deleting a "
        "division are different acts."
    ),
)
async def delete_org_node(
    node_id: NodeId, service: OrganizationServiceDep
) -> OrgNodeDeleteResponse:
    return OrgNodeDeleteResponse.from_result(await service.delete_node(node_id))


@router.post(
    "/nodes/{node_id}/members",
    response_model=OrgNodeRead,
    summary="File a person into a department",
    description=(
        "A move, not an add: a person belongs to exactly one department, so this "
        "removes any previous membership. The console is expected to confirm "
        "with the user first when the person already has one."
    ),
)
async def assign_org_member(
    node_id: NodeId, body: OrgMemberAssign, service: OrganizationServiceDep
) -> OrgNodeRead:
    return OrgNodeRead.from_view(await service.assign_member(node_id, body.user_id))


@router.delete(
    "/nodes/{node_id}/members/{user_id}",
    response_model=OrgNodeRead,
    summary="Remove a person from a department",
    description=(
        "Leaves them unassigned. Returns 404 if they are filed into a different "
        "department - silently succeeding there would let a stale screen unfile "
        "somebody the user was not looking at."
    ),
)
async def remove_org_member(
    node_id: NodeId,
    user_id: Annotated[uuid.UUID, Path(description="The person to remove.")],
    service: OrganizationServiceDep,
) -> OrgNodeRead:
    return OrgNodeRead.from_view(await service.remove_member(node_id, user_id))
