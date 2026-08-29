"""Add sibling ordering to org_nodes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-29

Sibling order in the org chart was an accident of `created_at, id` - the order
rows happened to be inserted in. Drag-and-drop reordering needs a real column
to write to. Added nullable with a default, backfilled from today's visible
order (so nothing appears to move on deploy), then locked to NOT NULL.

No index: every read either loads the whole tree or one parent's children,
both already served by the existing `parent_id` index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "org_nodes", sa.Column("position", sa.Integer(), nullable=False, server_default="0")
    )
    # Preserve today's visible order as the starting position, per parent.
    op.execute(
        """
        UPDATE org_nodes
        SET position = ranked.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY parent_id ORDER BY created_at, id
            ) - 1 AS rn
            FROM org_nodes
        ) AS ranked
        WHERE org_nodes.id = ranked.id
        """
    )
    # The server default did its job for the backfill; it isn't a
    # meaningful ordering value going forward, so drop it - every future
    # row gets an explicit position from the application.
    op.alter_column("org_nodes", "position", server_default=None)


def downgrade() -> None:
    op.drop_column("org_nodes", "position")
