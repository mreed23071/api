"""Persist whether a filtering decision was a fail-closed fallback.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-28

`FilterDecisionDto.is_fallback` has existed since the filtering agent was
written - true when the agent failed and the pipeline fell back to its
fail-closed default rather than making a real judgement - but it was only
ever held in memory for the duration of one run. `IngestionRunSummary` reads
it back as required, so every historical run's decisions failed to
deserialize: a 500 the browser reported as a CORS failure, since the error
escaped before CORS headers were attached.

`server_default=false` backfills every existing row to False, which is
honest about what happened: we never recorded the real value for them, and
"not a fallback" is the far more common case.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_run_decisions",
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("ingestion_run_decisions", "is_fallback")
