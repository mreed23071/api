"""Give ingestion runs a stable, unique workflow identifier.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-29

Two constraints, one idea: make every write that records a run idempotent.

`ingestion_runs.run_id` is the identifier the API mints at submission time and
derives the Temporal workflow id from. Until now it existed only in Temporal's
history and in the response body - the database knew runs by a surrogate id
nobody outside it could name, so the recording activity had no key to upsert on
and simply inserted. A worker that crashed between committing and acking its
activity therefore recorded the same run twice on retry. Unique here, it becomes
the conflict target that turns that second write into an update.

`uq_run_decision_message` does the same for the child rows. Duplicates from that
same crash-retry window may already exist in a live database, so the constraint
cannot just be added - the pairs are deduplicated first, keeping the earliest
row of each set.

Both are nullable/additive with respect to existing data: runs recorded before
this migration keep `NULL` for `run_id` rather than being assigned an invented
one, and `NULL`s do not collide under a unique index in Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Referenced by name from `workflows/activities.record_run`'s `ON CONFLICT`,
#: the same discipline `IDEMPOTENCY_CONSTRAINT` follows in the messaging
#: repository: a constraint an upsert names must not be renamable by accident.
RUN_ID_CONSTRAINT = "uq_ingestion_runs_run_id"
DECISION_CONSTRAINT = "uq_run_decision_message"


def upgrade() -> None:
    op.add_column("ingestion_runs", sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_unique_constraint(RUN_ID_CONSTRAINT, "ingestion_runs", ["run_id"])

    # Collapse any pre-existing duplicate verdicts before the constraint can
    # reject them. These are the residue of the crash-retry window described
    # above: the same run recorded twice, each copy carrying the same decision
    # set. Keeping the earliest `created_at` (ties broken by `id`, which is
    # total, so the ordering is deterministic) preserves the first write.
    op.execute(
        sa.text(
            """
            DELETE FROM ingestion_run_decisions
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY run_id, external_message_id
                               ORDER BY created_at, id
                           ) AS occurrence
                    FROM ingestion_run_decisions
                ) ranked
                WHERE ranked.occurrence > 1
            )
            """
        )
    )
    op.create_unique_constraint(
        DECISION_CONSTRAINT,
        "ingestion_run_decisions",
        ["run_id", "external_message_id"],
    )


def downgrade() -> None:
    op.drop_constraint(DECISION_CONSTRAINT, "ingestion_run_decisions", type_="unique")
    op.drop_constraint(RUN_ID_CONSTRAINT, "ingestion_runs", type_="unique")
    op.drop_column("ingestion_runs", "run_id")
