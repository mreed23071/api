"""v1 wire contracts for the department hierarchy.

The tree travels flat: a list of nodes, each carrying its own `parent_id`. The
client rebuilds the shape in one pass, which keeps the response the same size
and shape however deep the hierarchy gets, and makes re-parenting a one-field
update rather than a restructure.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.domains.organization.dto import DeletionResult, OrgNodeView


class OrgNodeRead(BaseModel):
    """One department, with the ids of the people filed into it."""

    id: uuid.UUID
    name: str
    subtitle: str | None = None
    parent_id: uuid.UUID | None = Field(default=None, description="Null when this node is a root.")
    member_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime

    @classmethod
    def from_view(cls, view: OrgNodeView) -> OrgNodeRead:
        return cls(
            id=view.id,
            name=view.name,
            subtitle=view.subtitle,
            parent_id=view.parent_id,
            member_ids=view.member_ids,
            created_at=view.created_at,
        )


class OrgNodeCreate(BaseModel):
    """A new department, optionally beneath an existing one."""

    name: str = Field(min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=255)
    parent_id: uuid.UUID | None = None


class OrgNodeUpdate(BaseModel):
    """A partial update, including the awkward case of moving to a root.

    `parent_id` has three meanings a client might intend - leave it alone, move
    it under X, or make it a root - and a plain optional field can only express
    two. `reparent` disambiguates: when it is true the `parent_id` sent is
    applied, including null.

    The validator rejects sending `parent_id` without `reparent`, rather than
    silently ignoring it. A caller that meant to move a department and did not
    should get an error, not a success that changed nothing.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=255)
    parent_id: uuid.UUID | None = None
    reparent: bool = Field(
        default=False,
        description="Set true to apply `parent_id`, including null to make this a root.",
    )

    @model_validator(mode="after")
    def _parent_requires_reparent(self) -> OrgNodeUpdate:
        if self.parent_id is not None and not self.reparent:
            raise ValueError(
                "parent_id was supplied without reparent=true, so it would be "
                "ignored. Set reparent=true to move this department."
            )
        return self


class OrgNodeDeleteResponse(BaseModel):
    """What deleting a department did to the departments beneath it."""

    id: uuid.UUID
    promoted: int = Field(
        description=(
            "How many sub-departments moved up to the deleted node's parent. "
            "Worth showing in a confirmation: deleting a leaf and deleting a "
            "division are different acts."
        )
    )

    @classmethod
    def from_result(cls, result: DeletionResult) -> OrgNodeDeleteResponse:
        return cls(id=result.id, promoted=result.promoted)


class OrgMemberAssign(BaseModel):
    """File a person into a department.

    A move, not an add: a person belongs to exactly one department, so this
    removes any previous membership. The client is expected to confirm with the
    user first when they are moving somebody who already has a department.
    """

    user_id: uuid.UUID
