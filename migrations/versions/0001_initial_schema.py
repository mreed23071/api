"""Initial schema: users, user_relations, messages (+ pgvector).

Revision ID: 0001
Revises:
Create Date: 2026-08-25

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Must match Settings.embedding_dim / the model's output width.
EMBEDDING_DIM = 384

PLATFORMS = ("slack", "github", "teams", "email", "linear", "other")

platform_enum = postgresql.ENUM(*PLATFORMS, name="platform", create_type=False)


def upgrade() -> None:
    # pgvector must exist before any `vector` column is declared. The image is
    # pgvector/pgvector, so the extension files are already on disk.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    platform_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("job_title", sa.String(length=255), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "user_relations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", platform_enum, nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("external_handle", sa.String(length=255), nullable=True),
        sa.Column("external_email", sa.String(length=320), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_relations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_relations"),
        # One external identity maps to exactly one internal user.
        sa.UniqueConstraint(
            "platform", "external_id", name="uq_user_relations_platform_external_id"
        ),
    )
    op.create_index("ix_user_relations_user_id", "user_relations", ["user_id"])
    op.create_index(
        "ix_user_relations_user_id_platform", "user_relations", ["user_id", "platform"]
    )

    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_relation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("platform", platform_enum, nullable=False),
        sa.Column("external_message_id", sa.String(length=255), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("filter_category", sa.String(length=64), nullable=True),
        sa.Column("filter_reason", sa.Text(), nullable=True),
        # Which prompt version produced the verdict above; without it a
        # retention decision cannot be explained after the policy changes.
        sa.Column("filter_prompt_version", sa.String(length=32), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sender_user_id"],
            ["users.id"],
            name="fk_messages_sender_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_relation_id"],
            ["user_relations.id"],
            name="fk_messages_sender_relation_id_user_relations",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        # Idempotency key for an at-least-once cron job.
        sa.UniqueConstraint(
            "platform", "external_message_id", name="uq_messages_platform_external_message_id"
        ),
    )
    op.create_index("ix_messages_sender_user_id", "messages", ["sender_user_id"])
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_sent_at", "messages", ["sent_at"])
    op.create_index(
        "ix_messages_sender_user_id_sent_at", "messages", ["sender_user_id", "sent_at"]
    )
    # ANN index for semantic search. Encoder output is L2-normalised, so cosine
    # is the matching operator class.
    op.create_index(
        "ix_messages_embedding_hnsw",
        "messages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_messages_embedding_hnsw", table_name="messages")
    op.drop_index("ix_messages_sender_user_id_sent_at", table_name="messages")
    op.drop_index("ix_messages_sent_at", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_index("ix_messages_sender_user_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_user_relations_user_id_platform", table_name="user_relations")
    op.drop_index("ix_user_relations_user_id", table_name="user_relations")
    op.drop_table("user_relations")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    platform_enum.drop(op.get_bind(), checkfirst=True)
    # The `vector` extension is intentionally left installed: other schemas in
    # the same database may depend on it.
