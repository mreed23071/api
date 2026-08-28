"""Record which platform each ingestion run belongs to.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-28

Ingestion is moving from one pipeline for every platform to one pipeline per
platform (GitHub, Slack, Teams so far). Without this column the run history
list can't tell which pipeline a given row belongs to - runs from every
platform would look identical.

Nullable, deliberately: runs recorded before this split predate the concept
and get `NULL` rather than an invented platform.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Matches the `platform` enum type created in 0001 - reused here, not
#: recreated, so `create_type=False`.
PLATFORMS = ("slack", "github", "teams", "email", "linear", "other")
platform_enum = postgresql.ENUM(*PLATFORMS, name="platform", create_type=False)


def upgrade() -> None:
    op.add_column("ingestion_runs", sa.Column("platform", platform_enum, nullable=True))


def downgrade() -> None:
    op.drop_column("ingestion_runs", "platform")
