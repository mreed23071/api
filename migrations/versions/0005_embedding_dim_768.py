"""Widen messages.embedding from 384 to 768 dimensions.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-28

Embedding generation moved from an in-process sentence-transformers model
(all-MiniLM-L6-v2, 384-d) to Ollama's nomic-embed-text (768-d), because an
inference service reached over the network is the shape this ships in - see
`app.shared.embeddings.base`.

pgvector fixes the dimension on the column so the HNSW index can be built, so
changing embedding model is always a migration. There is no arithmetic that
converts a 384-d vector into a comparable 768-d one, so existing vectors are
discarded rather than migrated: the column is dropped and re-added, which also
drops the HNSW index that depends on it. Re-embedding is what refills it -
`SEED_ON_STARTUP=1` plus an ingestion run per platform.

Deliberately not reversible without the same data loss in the other direction;
`downgrade` restores the 384-d column, empty.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_DIM = 384
NEW_DIM = 768

#: Must match `messages.py`, which names it explicitly - the model layer
#: declares this index, so the two have to agree.
INDEX_NAME = "ix_messages_embedding_hnsw"


def _swap_dimension(from_dim: int, to_dim: int) -> None:
    # Dropping the column takes the HNSW index with it; both are recreated
    # below. ALTER TYPE cannot change a vector's dimension in place.
    op.drop_index(INDEX_NAME, table_name="messages", if_exists=True)
    op.drop_column("messages", "embedding")
    op.add_column("messages", sa.Column("embedding", Vector(to_dim), nullable=True))
    op.create_index(
        INDEX_NAME,
        "messages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    # Every surviving row now claims an embedding model that did not produce
    # the (absent) vector beside it. Clearing it keeps provenance honest.
    op.execute("UPDATE messages SET embedding_model = NULL")


def upgrade() -> None:
    _swap_dimension(OLD_DIM, NEW_DIM)


def downgrade() -> None:
    _swap_dimension(NEW_DIM, OLD_DIM)
