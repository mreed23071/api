"""Organization context - the department tree and its membership.

The hierarchy is an adjacency list: a flat table plus `parent_id`. Re-parenting
is therefore a single-field update, and a client can rebuild the whole tree in
one pass. A materialised path would make subtree queries cheaper, and is the
first optimisation to reach for when authorization starts filtering rows by
department - but it is a denormalisation of this table, never a replacement.

Reading a model file
--------------------

These classes are both the Python objects the code passes around *and* the
definition of the database tables. A few conventions carry most of the meaning:

* `__tablename__` is the table's real name in Postgres.
* `Mapped[str]` is a type annotation that also declares the column's nullability:
  `Mapped[str]` is `NOT NULL`, `Mapped[str | None]` allows null. The type
  checker and the schema agree because they are the same declaration.
* `mapped_column(...)` carries what the annotation cannot - length, foreign key,
  default, index.
* `UUIDPrimaryKeyMixin` and `TimestampMixin` are shared column sets pulled in by
  inheritance, so `id`, `created_at` and `updated_at` are declared once for the
  whole application rather than on every table.
* `relationship(...)` is not a column. It is the Python-side link that lets you
  write `node.children`, and `lazy="raise"` means reading it without having
  explicitly asked for it raises rather than silently issuing another query -
  which is how a page that looks fast turns into a hundred queries.
* Changing anything here does *not* change the database. The schema moves only
  when a migration in `migrations/versions/` is written and applied.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import Base
from app.core.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domains.identity.models import User


class OrgNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One division, department or unit. `parent_id is None` means a root."""

    __tablename__ = "org_nodes"
    __table_args__ = (Index("ix_org_nodes_parent_id", "parent_id"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(255))

    #: SET NULL rather than CASCADE, deliberately. Deleting a department
    #: promotes its children to the parent - a decision the service layer makes
    #: with the whole tree in view. If a row is ever deleted outside that path,
    #: orphaning the children to roots is recoverable; cascading a whole subtree
    #: out of existence is not.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )

    parent: Mapped[OrgNode | None] = relationship(
        remote_side="OrgNode.id",
        back_populates="children",
        lazy="raise",
    )
    children: Mapped[list[OrgNode]] = relationship(
        back_populates="parent",
        lazy="raise",
    )
    memberships: Mapped[list[OrgNodeMember]] = relationship(
        back_populates="node",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrgNode {self.name}>"


class OrgNodeMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person's place in the hierarchy.

    `user_id` is unique, not part of a composite key: a person belongs to
    exactly one department. That invariant is what lets authorization collect a
    person's inherited grants with a single walk to the root instead of a graph
    traversal, so it is enforced by the database rather than by a convention
    somebody has to remember.
    """

    __tablename__ = "org_node_members"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_org_node_members_user_id"),
        Index("ix_org_node_members_org_node_id", "org_node_id"),
    )

    org_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    node: Mapped[OrgNode] = relationship(back_populates="memberships", lazy="raise")
    user: Mapped[User] = relationship(lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrgNodeMember node={self.org_node_id} user={self.user_id}>"
