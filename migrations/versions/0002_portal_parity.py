"""Portal parity: notes, org hierarchy, run history, and unattributed accounts.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-27

Brings the schema up to what the console already models. Three of the changes
are widenings of existing tables rather than new ones, and each exists because
the console can express a state the database could not:

* an account that belongs to nobody yet (`user_relations.user_id` nullable),
* a message that arrived on such an account (`messages.sender_user_id` nullable),
* a message that is a commit rather than a chat message (`messages.kind`).

The new tables are notes, the department hierarchy, and ingestion run history.

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _id_column() -> sa.Column:
    return sa.Column(
        "id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False
    )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    """Apply this migration.

    Alembic runs these in order, tracked by the `revision` / `down_revision`
    chain at the top of the file: this one declares `down_revision = "0001"`, so
    it runs after the initial schema and never twice. `alembic upgrade head`
    applies everything outstanding.

    `op` is the migration API - `op.add_column`, `op.create_table` and so on
    emit the corresponding DDL. Nothing here reads the model classes on purpose:
    a migration has to keep working after the models move on, so it spells the
    schema out in full.
    """
    # -- users: the profile fields the console already collects -------------
    op.add_column("users", sa.Column("address", sa.String(length=500), nullable=True))
    op.add_column("users", sa.Column("employment_start", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("employment_end", sa.Date(), nullable=True))

    # -- an external account can exist before anyone is attributed to it ----
    op.alter_column("user_relations", "user_id", existing_type=UUID, nullable=True)

    # -- and so can the messages that arrived on it -------------------------
    op.alter_column("messages", "sender_user_id", existing_type=UUID, nullable=True)
    op.add_column(
        "messages",
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="message"),
    )

    # -- notes: written by a person, unlike the generated summaries ---------
    op.create_table(
        "person_notes",
        _id_column(),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_person_notes"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_person_notes_user_id_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_person_notes_user_id_created_at", "person_notes", ["user_id", "created_at"]
    )

    # -- the department hierarchy, as an adjacency list ---------------------
    op.create_table(
        "org_nodes",
        _id_column(),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("subtitle", sa.String(length=255), nullable=True),
        sa.Column("parent_id", UUID, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_org_nodes"),
        # SET NULL, not CASCADE: deleting a department promotes its children,
        # a decision the service layer makes with the whole tree in view. If a
        # row is ever removed outside that path, orphaning children to roots is
        # recoverable; deleting a whole subtree is not.
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["org_nodes.id"],
            name="fk_org_nodes_parent_id_org_nodes",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_org_nodes_parent_id", "org_nodes", ["parent_id"])

    # -- membership: one department per person, enforced here ---------------
    #
    # The unique constraint is on `user_id` alone rather than on the pair. That
    # is the whole point: it makes "a person belongs to exactly one department"
    # a fact the database guarantees, which is what lets authorization collect
    # someone's inherited grants with a single walk to the root.
    op.create_table(
        "org_node_members",
        _id_column(),
        sa.Column("org_node_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_org_node_members"),
        sa.ForeignKeyConstraint(
            ["org_node_id"],
            ["org_nodes.id"],
            name="fk_org_node_members_org_node_id_org_nodes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_org_node_members_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", name="uq_org_node_members_user_id"),
    )
    op.create_index("ix_org_node_members_org_node_id", "org_node_members", ["org_node_id"])

    # -- ingestion run history ----------------------------------------------
    op.create_table(
        "ingestion_runs",
        _id_column(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("already_ingested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retained", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("persisted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("users_provisioned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filter_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filter_provider", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="success"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_runs"),
    )
    op.create_index("ix_ingestion_runs_started_at", "ingestion_runs", ["started_at"])

    op.create_table(
        "ingestion_run_decisions",
        _id_column(),
        sa.Column("run_id", UUID, nullable=False),
        # Not a foreign key to messages: a discarded message is never persisted,
        # so half of these rows would dangle by design.
        sa.Column("external_message_id", sa.String(length=255), nullable=False),
        sa.Column("keep", sa.Boolean(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_run_decisions"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_runs.id"],
            name="fk_ingestion_run_decisions_run_id_ingestion_runs",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_ingestion_run_decisions_run_id", "ingestion_run_decisions", ["run_id"]
    )


def downgrade() -> None:
    """Undo this migration, in the reverse order of `upgrade`.

    Reverse order matters: a table cannot be dropped while another still points
    a foreign key at it, so dependents go first.

    Two of these steps can legitimately fail, and should. Narrowing
    `sender_user_id` and `user_id` back to NOT NULL is impossible if any
    unattributed rows exist by then - and a downgrade that silently deleted
    somebody's messages to make room would be far worse than one that refuses
    to run.
    """
    op.drop_index("ix_ingestion_run_decisions_run_id", table_name="ingestion_run_decisions")
    op.drop_table("ingestion_run_decisions")
    op.drop_index("ix_ingestion_runs_started_at", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")

    op.drop_index("ix_org_node_members_org_node_id", table_name="org_node_members")
    op.drop_table("org_node_members")
    op.drop_index("ix_org_nodes_parent_id", table_name="org_nodes")
    op.drop_table("org_nodes")

    op.drop_index("ix_person_notes_user_id_created_at", table_name="person_notes")
    op.drop_table("person_notes")

    op.drop_column("messages", "kind")
    # Narrowing back fails if any unattributed rows exist - deliberately. A
    # downgrade that silently deleted messages or accounts would be worse than
    # one that refuses to run.
    op.alter_column("messages", "sender_user_id", existing_type=UUID, nullable=False)
    op.alter_column("user_relations", "user_id", existing_type=UUID, nullable=False)

    op.drop_column("users", "employment_end")
    op.drop_column("users", "employment_start")
    op.drop_column("users", "address")
