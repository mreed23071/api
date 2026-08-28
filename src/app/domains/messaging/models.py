"""Messaging context - the retained message corpus and its vectors."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.core.db.base import Base
from app.core.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domains.identity.models import Platform, platform_enum

if TYPE_CHECKING:
    from app.domains.identity.models import User, UserRelation

#: Fixed at import time: the column width has to match the migration, so
#: changing models means changing EMBEDDING_DIM *and* writing a migration.
EMBEDDING_DIM = get_settings().embedding_dim


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One retained message plus its locally-generated embedding.

    Two sender foreign keys, deliberately:

    * `sender_user_id`     - the resolved internal person (what we summarise by).
    * `sender_relation_id` - the exact platform identity the message arrived on,
      so provenance survives even after identities are merged or re-mapped.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_message_id",
            name="uq_messages_platform_external_message_id",
        ),
        Index("ix_messages_sender_user_id_sent_at", "sender_user_id", "sent_at"),
        # Approximate nearest-neighbour index for semantic search. Vectors are
        # L2-normalised by the encoder, so cosine is the right operator class.
        Index(
            "ix_messages_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    #: Nullable, because a message can arrive on an account nobody has
    #: attributed yet. It gains a sender when the account is linked, which
    #: is what "reattributes every historical message" means in practice.
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    sender_relation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_relations.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: "message" | "commit". A plain string, not a native enum: this
    #: vocabulary grows every time a connector is added, and widening an
    #: enum type costs a migration that a string does not. The API schema is
    #: where the closed set is enforced.
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="message")
    platform: Mapped[Platform] = mapped_column(platform_enum, nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(255), index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255))

    #: Why the filtering agent kept this message - auditable, and useful when
    #: tuning INGESTION_FILTER_SYSTEM_PROMPT.
    filter_category: Mapped[str | None] = mapped_column(String(64))
    filter_reason: Mapped[str | None] = mapped_column(Text)
    #: Which prompt version produced the verdict above. Without this a
    #: retention decision cannot be explained after the policy changes.
    filter_prompt_version: Mapped[str | None] = mapped_column(String(32))

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    #: Provider-specific payload. Commit detail for a `kind="commit"` message
    #: lives here rather than in columns: it is one connector's shape, and
    #: modelling it as columns would put GitHub's vocabulary in every row.
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    sender: Mapped[User | None] = relationship(back_populates="messages", lazy="raise")
    sender_relation: Mapped[UserRelation | None] = relationship(lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message {self.platform}:{self.external_message_id}>"
