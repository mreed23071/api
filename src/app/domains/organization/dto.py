"""Internal contracts published by the organization context.

Not wire types. The API layer maps these to versioned schemas; a change here is
an internal refactor, a change to `app.api.v1.schemas` is a breaking API change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class NewOrgNode:
    """A department to create."""

    name: str
    subtitle: str | None = None
    parent_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class OrgNodePatch:
    """A partial update.

    `parent_id` is paired with a `reparent` flag rather than standing alone,
    because `None` is a meaningful value here - it means "make this a root".
    Without the flag there is no way to tell "promote to a root" apart from
    "leave the parent alone", and the two are very different edits.
    """

    name: str | None = None
    subtitle: str | None = None
    parent_id: uuid.UUID | None = None
    reparent: bool = False


@dataclass(frozen=True, slots=True)
class OrgNodeView:
    """One node, flattened with its membership.

    The tree is returned as a flat list plus `parent_id`. The client rebuilds it
    in one pass, which keeps the response shape stable no matter how deep the
    hierarchy gets.
    """

    id: uuid.UUID
    name: str
    subtitle: str | None
    parent_id: uuid.UUID | None
    created_at: datetime
    member_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """What deleting a node did.

    `promoted` is the count of children that moved up to the deleted node's
    parent. The caller shows it, because "deleted" and "deleted, and four
    departments moved" deserve different confirmations.
    """

    id: uuid.UUID
    promoted: int
